"""Import every quad package module to catch syntax/import regressions."""

import importlib
import pkgutil

import quad


def test_all_modules_import():
    """Every module under ``quad.*`` must import without error."""
    names = [
        m.name
        for m in pkgutil.walk_packages(quad.__path__, prefix="quad.")
        if m.name != "quad.__main__"
    ]
    assert len(names) > 50, f"suspiciously few modules: {len(names)}"
    for name in names:
        importlib.import_module(name)
