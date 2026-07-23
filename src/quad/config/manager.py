"""Quad configuration manager.

Provides 4-layer configuration merge:
  1. config/config.default.yaml (shipped defaults)
  2. config/config.local.yaml (user overrides, gitignored)
  3. Environment variables (QUAD_* and BINANCE_*)
  4. Runtime overrides (via set())

All layers merge with layer 4 being highest priority.
Dot-notation key access: config.get("risk.max_position_size")
Hot-reload support via on_change callbacks.

Thread-safe: all reads/writes to the resolved config are protected by a lock.
"""

from __future__ import annotations

import copy
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable

import structlog
import yaml
from dotenv import find_dotenv, load_dotenv

from .schema import validate_config

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Environment variables that map directly to config keys.
# These define the authoritative mapping for well-known env vars.
ENV_VAR_MAP: dict[str, str] = {
    "QUAD_LOG_LEVEL": "logging.level",
    "QUAD_LOG_FORMAT": "logging.format",
    "QUAD_LOG_FILE": "logging.file.path",
    "QUAD_HEALTH_PORT": "monitoring.health_server.port",
    "QUAD_MODE": "_mode",
    "QUAD_DRY_RUN": "_dry_run",
    "QUAD_DEFAULT_STRATEGY": "trading.default_strategy",
    "QUAD_MAX_CYCLE_INTERVAL": "trading.max_cycle_interval",
    "QUAD_DSN": "persistence.dsn",
    "BINANCE_API_KEY": "exchange.api_key",
    "BINANCE_API_SECRET": "exchange.api_secret",
    "BINANCE_TESTNET": "exchange.testnet",
    "QUAD_AI_ENABLED": "ai.enabled",
    "QUAD_AI_MODEL": "ai.model",
    "QUAD_AI_TIMEOUT": "ai.timeout",
    "QUAD_AI_MAX_REQUESTS_PER_DAY": "ai.max_requests_per_day",
    "GROQ_API_KEY": "ai.api_key",
    "QUAD_TRADINGVIEW_WEBHOOK_ENABLED": "tradingview_webhook.enabled",
    "QUAD_TRADINGVIEW_WEBHOOK_PORT": "tradingview_webhook.port",
    "QUAD_TRADINGVIEW_WEBHOOK_SECRET": "tradingview_webhook.secret",
}

# Default config directory search order
DEFAULT_CONFIG_DIRS: list[Path] = [
    Path.home() / ".quad" / "config",
    Path.cwd() / "config",
]

# Config file names
DEFAULT_CONFIG_FILE = "config.default.yaml"
LOCAL_CONFIG_FILE = "config.local.yaml"


# ============================================================================
# Public API
# ============================================================================

class ConfigManager:
    """Configuration manager with layered merge, dot-notation access,
    hot-reload callbacks, and thread safety.

    The resolved configuration is built from four layers (lower number =
    lower priority):

        1.  config/config.default.yaml (shipped defaults)
        2.  config/config.local.yaml (user overrides, gitignored)
        3.  Environment variables (QUAD_* and BINANCE_*)
        4.  Runtime overrides (via set())

    Args:
        config_dir: Optional path to the configuration directory. If not
            provided, the manager searches in order: ``$QUAD_CONFIG_DIR``,
            ``~/.quad/config/``, ``./config/``.

    Raises:
        FileNotFoundError: If ``config.default.yaml`` cannot be found.
        yaml.YAMLError: If the default config file is malformed.
    """

    def __init__(self, config_dir: str | Path | None = None) -> None:
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[str, Any, Any], None]] = []
        self._runtime_overrides: dict[str, Any] = {}

        # Determine config directory
        self._config_dir = self._resolve_config_dir(config_dir)
        logger.info("config_manager_init", config_dir=str(self._config_dir))

        # Load .env file from config directory or parent dirs
        self._load_env_file()

        # Load layers (stored individually for debugging/diffing)
        self._layers: dict[str, dict[str, Any]] = {
            "defaults": {},
            "local": {},
            "env": {},
        }

        # Build the resolved config
        self._config: dict[str, Any] = {}
        self._load_all_layers()

        log = logger.bind(config_dir=str(self._config_dir))
        log.info(
            "config_loaded",
            has_local=self._layers["local"] != {},
            has_env_overrides=self._layers["env"] != {},
        )

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        """Get a config value by dot-notation key.

        Args:
            key: Dot-notation path, e.g. ``"risk.max_position_size"``.
            default: Value returned if the key is not found.

        Returns:
            The config value at ``key``, or ``default`` if not found.
        """
        with self._lock:
            return _dot_get(self._config, key, default)

    def set(self, key: str, value: Any) -> None:
        """Set a runtime override value.

        This is the highest-priority layer. Changing a value triggers all
        registered ``on_change`` callbacks with ``(key, old_value, new_value)``.

        Args:
            key: Dot-notation path, e.g. ``"risk.max_position_size"``.
            value: The value to set. Must be JSON-serializable.
        """
        old_value: Any = _MISSING

        with self._lock:
            old_value = _dot_get(self._config, key, _MISSING)
            _dot_set(self._config, key, value)
            _dot_set(self._runtime_overrides, key, value)

        # Fire callbacks outside the lock to avoid deadlocks
        if old_value is _MISSING or old_value != value:
            log = logger.bind(key=key, old=old_value, new=value)
            log.info("config_override_set")
            self._fire_callbacks(key, old_value, value)

    def get_section(self, prefix: str) -> dict[str, Any]:
        """Return the configuration subtree under ``prefix`` as a flat dict.

        Args:
            prefix: Dot-notation prefix, e.g. ``"risk"``.

        Returns:
            A shallow copy of the subtree at ``prefix``, or an empty dict if
            the prefix is not found.
        """
        with self._lock:
            section = _dot_get(self._config, prefix, _MISSING)
            if section is _MISSING:
                return {}
            if isinstance(section, dict):
                return dict(section)
            return {}

    def reload(self) -> None:
        """Re-read config files from disk and re-apply all layers.

        Runtime overrides set via ``set()`` are preserved and re-applied
        on top of the newly loaded values. Callbacks are fired for any keys
        whose values changed during the reload.

        Raises:
            yaml.YAMLError: If ``config.default.yaml`` is malformed.
        """
        old_config: dict[str, Any] = {}

        with self._lock:
            old_config = copy.deepcopy(self._config)
            self._load_all_layers()

        # Fire callbacks for changed keys
        changes = _dict_diff(old_config, self._config)
        for key, (old_val, new_val) in sorted(changes.items()):
            log = logger.bind(key=key, old=old_val, new=new_val)
            log.info("config_reload_changed")
            self._fire_callbacks(key, old_val, new_val)

        if not changes:
            logger.info("config_reload_no_changes")
        else:
            changed_count = len(changes)
            logger.info("config_reload_complete", changed_keys=changed_count)

    def to_dict(self) -> dict[str, Any]:
        """Return the full resolved configuration as a nested dict.

        Returns:
            A deep copy of the resolved configuration.
        """
        with self._lock:
            return copy.deepcopy(self._config)

    def on_change(
        self, callback: Callable[[str, Any, Any], None]
    ) -> None:
        """Register a hot-reload listener.

        The callback is invoked with ``(key, old_value, new_value)`` whenever
        a config value changes via ``set()`` or ``reload()``.

        Args:
            callback: Function accepting ``(key: str, old_value: Any,
                new_value: Any)``.
        """
        with self._lock:
            self._callbacks.append(callback)

    def get_mode(self) -> str:
        """Return the current trading mode.

        Returns:
            ``"paper"``, ``"live"``, or ``"dry_run"``, retrieved from
            the ``_mode`` internal key (set via ``QUAD_MODE`` env var
            or runtime override). Defaults to ``"paper"``.
        """
        return str(self.get("_mode", "paper"))

    def get_default_strategy(self) -> str:
        """Return the configured default strategy name.

        Returns:
            The strategy name from ``trading.default_strategy``.
        """
        return str(self.get("trading.default_strategy", "cash_secured_put"))

    # ------------------------------------------------------------------
    # Container protocol
    # ------------------------------------------------------------------

    def __getitem__(self, key: str) -> Any:
        """Support ``config["key"]`` syntax.

        Raises:
            KeyError: If the key is not found in the resolved config.
        """
        value = self.get(key, _MISSING)
        if value is _MISSING:
            raise KeyError(f"Config key not found: '{key}'")
        return value

    def __contains__(self, key: str) -> bool:
        """Support ``"key" in config`` syntax."""
        return self.get(key, _MISSING) is not _MISSING

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_config_dir(
        self, config_dir: str | Path | None
    ) -> Path:
        """Resolve the configuration directory path.

        Priority:
            1. ``config_dir`` parameter
            2. ``$QUAD_CONFIG_DIR`` environment variable
            3. ``~/.quad/config/``
            4. ``./config/`` (project root)
        """
        if config_dir is not None:
            return Path(config_dir).expanduser().resolve()

        env_dir = os.environ.get("QUAD_CONFIG_DIR")
        if env_dir:
            return Path(env_dir).expanduser().resolve()

        for candidate in DEFAULT_CONFIG_DIRS:
            resolved = candidate.expanduser().resolve()
            default_path = resolved / DEFAULT_CONFIG_FILE
            if default_path.exists():
                logger.debug(
                    "config_dir_discovered",
                    path=str(resolved),
                )
                return resolved

        # Fall back to project root ./config/
        return (Path.cwd() / "config").resolve()

    def _load_env_file(self) -> None:
        """Load .env file from the config directory or parent directories.

        Uses ``dotenv.find_dotenv()`` to search upward from the config
        directory, then ``dotenv.load_dotenv()`` to populate
        ``os.environ``.
        """
        env_path = find_dotenv(
            filename=".env",
            raise_error_if_not_found=False,
            usecwd=False,
        )
        if not env_path:
            # Try specific paths
            for candidate in (
                self._config_dir / ".env",
                self._config_dir.parent / ".env",
                Path.cwd() / ".env",
            ):
                if candidate.exists():
                    env_path = str(candidate)
                    break

        if env_path:
            loaded = load_dotenv(env_path, override=False)
            logger.info(
                "dotenv_loaded",
                path=env_path,
                loaded=loaded,
            )
        else:
            logger.info("dotenv_not_found")

    def _load_all_layers(self) -> None:
        """Load and merge all four configuration layers.

        Internal state updated:
            - ``_layers["defaults"]``
            - ``_layers["local"]``
            - ``_layers["env"]``
            - ``_config`` (merged result)
        """
        # Layer 1: Defaults
        defaults_path = self._config_dir / DEFAULT_CONFIG_FILE
        if not defaults_path.exists():
            raise FileNotFoundError(
                f"Default configuration file not found: {defaults_path}. "
                "Ensure config/config.default.yaml exists in the project "
                "or set QUAD_CONFIG_DIR."
            )
        self._layers["defaults"] = _load_yaml(defaults_path)
        self._layers["defaults"] = _recursive_expand_env_vars(
            self._layers["defaults"]
        )
        logger.debug(
            "layer_loaded",
            layer="defaults",
            path=str(defaults_path),
        )

        # Layer 2: Local overrides
        local_path = self._config_dir / LOCAL_CONFIG_FILE
        if local_path.exists():
            self._layers["local"] = _load_yaml(local_path)
            self._layers["local"] = _recursive_expand_env_vars(
                self._layers["local"]
            )
            logger.debug(
                "layer_loaded",
                layer="local",
                path=str(local_path),
            )
        else:
            self._layers["local"] = {}
            logger.debug(
                "local_config_not_found",
                path=str(local_path),
            )

        # Layer 3: Environment variables
        self._layers["env"] = _apply_env_overrides({})

        # Merge: defaults <- local <- env
        merged: dict[str, Any] = copy.deepcopy(self._layers["defaults"])
        merged = _deep_merge(merged, copy.deepcopy(self._layers["local"]))
        merged = _deep_merge(merged, copy.deepcopy(self._layers["env"]))

        # Layer 4: Re-apply runtime overrides on top
        merged = _deep_merge(merged, copy.deepcopy(self._runtime_overrides))

        self._config = merged

        # Validate config after merge
        is_valid, errors = validate_config(self._config)
        if not is_valid:
            for error in errors:
                logger.warning("config_validation_warning", error=error)

    def _fire_callbacks(
        self, key: str, old_value: Any, new_value: Any
    ) -> None:
        """Invoke all registered ``on_change`` callbacks."""
        # Work on a snapshot of the callback list to avoid issues if a
        # callback modifies the list during iteration.
        callbacks = list(self._callbacks)
        for cb in callbacks:
            try:
                cb(key, old_value, new_value)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "config_callback_error",
                    key=key,
                )


# ============================================================================
# Helper Functions
# ============================================================================

_MISSING: Any = object()
"""Sentinel used to distinguish ``None`` from an absent value."""

_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into ``base``.

    For keys present in both dicts:
      - If both values are dicts, merge recursively.
      - Otherwise, the override value wins.

    Args:
        base: The base dictionary (lower priority).
        override: The override dictionary (higher priority).

    Returns:
        A new dict with the merged result. Neither input is mutated.
    """
    result = copy.deepcopy(base)

    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)

    return result


def _load_yaml(path: Path) -> dict[str, Any]:
    """Safely load a YAML file and return the parsed dict.

    Uses ``yaml.SafeLoader`` to prevent arbitrary code execution from
    malicious YAML files.

    Args:
        path: Path to the YAML file.

    Returns:
        Parsed dictionary.

    Raises:
        FileNotFoundError: If the file does not exist.
        yaml.YAMLError: If the file contains invalid YAML.
    """
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    path_str = str(path)
    with open(path_str, "r", encoding="utf-8") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as exc:
            logger.error("yaml_parse_error", path=path_str, error=str(exc))
            raise

    if data is None:
        return {}

    if not isinstance(data, dict):
        msg = (
            f"YAML file {path_str} must contain a top-level mapping (dict), "
            f"got {type(data).__name__}"
        )
        logger.error("yaml_type_error", path=path_str)
        raise TypeError(msg)

    return dict(data)


def _apply_env_overrides(config: dict[str, Any]) -> dict[str, Any]:
    """Scan environment variables and inject them into the config tree.

    Scans for ``QUAD_*`` and ``BINANCE_*`` environment variables. Well-known
    variables (defined in ``ENV_VAR_MAP``) are mapped to their exact config
    keys. Unknown ``QUAD_*`` variables are mapped heuristically by stripping
    the ``QUAD_`` prefix, lowercasing, and splitting on ``_``.

    Type coercion is attempted for booleans, integers, and floats.

    Args:
        config: The configuration dictionary to augment (modified in place).

    Returns:
        The same ``config`` dict with env vars applied.
    """
    for env_name, env_value in os.environ.items():
        if not env_value:
            continue

        config_key: str | None = None

        # Check well-known mapping first
        if env_name in ENV_VAR_MAP:
            config_key = ENV_VAR_MAP[env_name]
        elif env_name.startswith("QUAD_") or env_name.startswith("BINANCE_"):
            config_key = _env_to_config_key(env_name)

        if config_key is None:
            continue

        typed_value = _coerce_env_value(env_value)
        _dot_set(config, config_key, typed_value)

    return config


def _env_to_config_key(env_name: str) -> str:
    """Convert an environment variable name to a dot-notation config key.

    Examples:
        ``"QUAD_RISK_MAX_POSITION_SIZE"`` -> ``"risk.max_position_size"``
        ``"QUAD_LOG_LEVEL"`` -> ``"logging.level"``

    Args:
        env_name: The environment variable name.

    Returns:
        Dot-notation config key.
    """
    # Handle BINANCE_ prefix
    if env_name.startswith("BINANCE_"):
        key = env_name[len("BINANCE_"):]
    elif env_name.startswith("QUAD_"):
        key = env_name[len("QUAD_"):]
    else:
        return env_name.lower().replace("__", ".").replace("_", ".")

    # Special handling: some two-part prefixes map to specific sections
    # e.g., QUAD_LOG_LEVEL -> logging.level (not log.level)
    # This is handled by the ENV_VAR_MAP for well-known vars.
    # For unknown vars, naive heuristic: lowercase and replace _ with .

    parts = key.lower().split("_")

    # Common section renames for readability
    section_renames = {
        "log": "logging",
        "db": "persistence",
        "health": "monitoring.health_server",
        "telegram": "telegram",
    }

    if parts and parts[0] in section_renames:
        prefix = section_renames[parts[0]]
        suffix = ".".join(parts[1:]) if len(parts) > 1 else ""
        return f"{prefix}.{suffix}" if suffix else prefix

    return ".".join(parts)


def _coerce_env_value(value: str) -> Any:
    """Coerce an environment variable string to the most appropriate type.

    Attempts conversion in order: bool -> int -> float -> str.
    Booleans match case-insensitively: ``true``/``1``/``yes``, ``false``/``0``/``no``.

    Args:
        value: The raw environment variable string.

    Returns:
        The coerced value (bool, int, float, or str).
    """
    lower = value.lower().strip()

    if lower in ("true", "1", "yes", "on"):
        return True
    if lower in ("false", "0", "no", "off"):
        return False

    # Try integer
    try:
        return int(value)
    except (ValueError, TypeError):
        pass

    # Try float
    try:
        return float(value)
    except (ValueError, TypeError):
        pass

    return value


def _dot_get(d: dict, key: str, default: Any = None) -> Any:
    """Recursive dot-notation lookup into a nested dict.

    Args:
        d: The nested dictionary.
        key: Dot-notation path, e.g. ``"risk.max_position_size"``.
        default: Value returned if a path segment is missing.

    Returns:
        The value at ``key``, or ``default`` if not found.
    """
    if not key:
        return default

    parts = key.split(".")
    current: Any = d

    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return default

    return current


def _dot_set(d: dict, key: str, value: Any) -> None:
    """Recursive dot-notation setter into a nested dict.

    Creates intermediate dicts as needed.

    Args:
        d: The nested dictionary (modified in place).
        key: Dot-notation path, e.g. ``"risk.max_position_size"``.
        value: The value to set.
    """
    if not key:
        return

    parts = key.split(".")
    current = d

    for i, part in enumerate(parts[:-1]):
        if part not in current or not isinstance(current[part], dict):
            current[part] = {}
        current = current[part]

    current[parts[-1]] = value


def _recursive_expand_env_vars(value: Any) -> Any:
    """Recursively expand ``${VAR_NAME}`` patterns in all string values.

    Uses ``os.path.expandvars()`` for the expansion and
    ``os.path.expanduser()`` to resolve ``~``.

    Args:
        value: The value to process (dict, list, str, or scalar).

    Returns:
        The processed value with env vars expanded in all strings.
    """
    if isinstance(value, str):
        expanded = os.path.expandvars(value)
        expanded = os.path.expanduser(expanded)
        return expanded

    if isinstance(value, dict):
        return {
            k: _recursive_expand_env_vars(v) for k, v in value.items()
        }

    if isinstance(value, list):
        return [_recursive_expand_env_vars(item) for item in value]

    return value


def _dict_diff(
    old: dict[str, Any],
    new: dict[str, Any],
    _prefix: str = "",
) -> dict[str, tuple[Any, Any]]:
    """Compare two nested dicts and return changed keys with old/new values.

    Recursively walks both dicts and produces a flat dict of
    ``{dot_notation_key: (old_value, new_value)}`` for keys whose values
    differ.

    Args:
        old: The old configuration dict.
        new: The new configuration dict.
        _prefix: Internal recursion prefix (do not pass externally).

    Returns:
        Flat dict of changes.
    """
    changes: dict[str, tuple[Any, Any]] = {}
    all_keys: set[str] = set(old.keys()) | set(new.keys())

    for key in all_keys:
        dot_key = f"{_prefix}.{key}" if _prefix else key

        if key not in old:
            # Key was added
            changes[dot_key] = (_MISSING, copy.deepcopy(new[key]))
        elif key not in new:
            # Key was removed
            changes[dot_key] = (copy.deepcopy(old[key]), _MISSING)
        elif isinstance(old[key], dict) and isinstance(new[key], dict):
            # Recurse into nested dicts
            nested = _dict_diff(old[key], new[key], _prefix=dot_key)
            changes.update(nested)
        elif old[key] != new[key]:
            # Value changed
            changes[dot_key] = (
                copy.deepcopy(old[key]),
                copy.deepcopy(new[key]),
            )

    return changes
