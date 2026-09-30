"""Tenant worker supervisor (Phase 4 execution plane).

One OS process per active tenant, each running ``python -m quad`` with a
generated config file (0600) carrying the tenant's decrypted Bybit keys and
overrides.  The supervisor starts workers on exchange-connect, stops them on
disconnect, and reaps/re restarts crashed workers with a bounded crash-loop
guard (more than *max_restarts* crashes inside *restart_window_s* marks the
worker failed until the next explicit ensure).
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

from quad.persistence import DatabaseManager

from .worker import WorkerConfigError, build_worker_config

logger = structlog.get_logger(__name__)


@dataclass
class WorkerInfo:
    tenant_uuid: str
    config_path: str = ""
    pid: int | None = None
    state: str = "stopped"  # running | stopped | failed
    restarts: int = 0
    last_start: float = 0.0
    last_exit: float = 0.0
    last_error: str = ""
    last_heartbeat: float = 0.0
    _process: Any = field(default=None, repr=False)
    _crash_times: list = field(default_factory=list, repr=False)


class Supervisor:
    """Owns tenant worker processes for one control-plane instance."""

    def __init__(
        self,
        registry_db: DatabaseManager,
        base_config: dict[str, Any] | None = None,
        base_config_path: str = "config/config.yaml",
        worker_dir: str = "data/workers",
        max_restarts: int = 5,
        restart_window_s: int = 600,
        worker_argv: list[str] | None = None,
    ) -> None:
        self._db = registry_db
        self._base_config = base_config
        self._base_config_path = base_config_path
        self._worker_dir = Path(worker_dir)
        self._max_restarts = max_restarts
        self._restart_window = restart_window_s
        # Test hook: full argv tail after the python binary (default runs the bot).
        self._worker_argv = worker_argv
        self._workers: dict[str, WorkerInfo] = {}
        self._log = logger.bind(component="supervisor")

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def _load_base_config(self) -> dict[str, Any]:
        if self._base_config is not None:
            return self._base_config
        path = Path(self._base_config_path)
        if not path.exists():
            return {}
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    def _write_config_file(self, tenant_uuid: str, config: dict[str, Any]) -> str:
        self._worker_dir.mkdir(parents=True, exist_ok=True)
        path = self._worker_dir / f"{tenant_uuid}.yaml"
        fd = os.open(
            str(path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                yaml.safe_dump(config, fh, default_flow_style=False)
        except Exception:
            # fdopen owns fd: closing it again would double-close.
            # Remove any partial config (holds decrypted keys), then raise.
            try:
                os.remove(path)
            except OSError:
                pass
            raise
        return str(path)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def ensure(self, tenant_uuid: str, _from_sweep: bool = False) -> WorkerInfo:
        """Build config and start the worker unless already running."""
        # Headless safety: reap exited workers on every ensure so a dead
        # worker is never reported (or left) running.
        if not _from_sweep:
            try:
                await self.sweep()
            except Exception:
                self._log.warning("ensure_sweep_failed", tenant=tenant_uuid)
        info = self._workers.get(tenant_uuid)
        if info is not None:
            self._poll(info)
            if info.state == "running":
                info.last_heartbeat = time.time()
                return info
            if info.state == "failed":
                # Explicit ensure clears a previous crash-loop verdict.
                info.state = "stopped"
                info.restarts = 0
                info._crash_times = []
        else:
            info = WorkerInfo(tenant_uuid=tenant_uuid)
            self._workers[tenant_uuid] = info

        try:
            config = await build_worker_config(
                self._db, tenant_uuid, self._load_base_config()
            )
        except WorkerConfigError as exc:
            info.state = "failed"
            info.last_error = str(exc)
            self._log.warning(
                "worker_config_failed", tenant=tenant_uuid, error=str(exc)
            )
            return info

        info.config_path = self._write_config_file(tenant_uuid, config)
        if info.last_start:
            info.restarts += 1
        self._spawn(info)
        return info

    def _spawn(self, info: WorkerInfo) -> None:
        if self._worker_argv is not None:
            cmd = [sys.executable, *self._worker_argv]
        else:
            cmd = [sys.executable, "-m", "quad", "--config", info.config_path]
        try:
            proc = subprocess.Popen(cmd, cwd=str(Path.cwd()))  # noqa: S603 trusted argv
        except Exception as exc:
            info.state = "failed"
            info.last_error = str(exc)
            self._log.exception("worker_spawn_failed", tenant=info.tenant_uuid)
            return
        info._process = proc
        info.pid = proc.pid
        info.state = "running"
        info.last_start = time.time()
        self._log.info("worker_started", tenant=info.tenant_uuid, pid=proc.pid)

    def _poll(self, info: WorkerInfo) -> None:
        proc = info._process
        if proc is None or info.state != "running":
            return
        if proc.poll() is None:
            info.last_heartbeat = time.time()
            return
        info.last_exit = time.time()
        info._crash_times.append(info.last_exit)
        info._crash_times = [
            t for t in info._crash_times if t > info.last_exit - self._restart_window
        ]
        info._process = None
        info.pid = None
        if len(info._crash_times) > self._max_restarts:
            info.state = "failed"
            info.last_error = (
                f"crashed {len(info._crash_times)}x in {self._restart_window}s"
            )
            self._log.error("worker_crash_loop", tenant=info.tenant_uuid)
        else:
            info.state = "stopped"
            self._log.warning(
                "worker_exited", tenant=info.tenant_uuid, code=proc.returncode
            )
        # Crash path must also drop the rendered config FILE (decrypted keys),
        # not just stop(): a crashed worker never reaches stop().  Keep
        # info.config_path so sweep() still restarts via ensure(), which
        # rewrites the file before spawning.
        try:
            if info.config_path:
                os.remove(info.config_path)
        except OSError:
            pass

    async def stop(self, tenant_uuid: str, timeout: float = 20.0) -> WorkerInfo:
        """Stop a worker (SIGTERM, then SIGKILL after *timeout*)."""
        info = self._workers.get(tenant_uuid)
        if info is None:
            info = WorkerInfo(tenant_uuid=tenant_uuid)
            self._workers[tenant_uuid] = info
            return info
        self._poll(info)
        proc = info._process
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            self._log.info("worker_stopped", tenant=tenant_uuid)
        info._process = None
        info.pid = None
        if info.state == "running":
            info.state = "stopped"
        # Remove the rendered config (holds decrypted keys) on stop.
        try:
            if info.config_path:
                os.remove(info.config_path)
        except OSError:
            pass
        info.config_path = ""
        return info

    async def stop_all(self) -> None:
        for tenant_uuid in list(self._workers):
            await self.stop(tenant_uuid)

    async def sweep(self) -> dict[str, str]:
        """Reap exited workers; restart crash-budgeted ones. Returns states."""
        from quad.persistence.repositories import TenantRepository

        result = {}
        for tenant_uuid, info in list(self._workers.items()):
            self._poll(info)
            if info.state == "stopped" and info.config_path:
                # Unexpected exit with budget left — restart via ensure,
                # unless the tenant was halted/suspended (fail closed).
                try:
                    tenant = await TenantRepository(self._db).get_by_uuid(tenant_uuid)
                except Exception:
                    tenant = None
                if tenant is not None and tenant.status != "active":
                    info.state = "stopped"
                    info.last_error = f"tenant {tenant.status}; not restarting"
                else:
                    await self.ensure(tenant_uuid, _from_sweep=True)
            result[tenant_uuid] = info.state
        return result

    def status(self, tenant_uuid: str | None = None) -> dict:
        if tenant_uuid is not None:
            info = self._workers.get(tenant_uuid)
            if info is None:
                return {"state": "stopped"}
            self._poll(info)
            return {
                "state": info.state,
                "pid": info.pid,
                "restarts": info.restarts,
                "last_error": info.last_error,
                "last_heartbeat": info.last_heartbeat,
            }
        return {
            uuid: {"state": i.state, "pid": i.pid} for uuid, i in self._workers.items()
        }
