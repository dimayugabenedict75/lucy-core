"""Helpers shared by tool modules (leading underscore: never treated as a tool module)."""
from pathlib import Path

from lucy import paths as _paths
from lucy.server import file_guard


def resolve_path(file_path: str):
    """Resolve what the model typed to a real path under SAFE_ROOT (None if outside). See file_guard.resolve_path."""
    return file_guard.resolve_path(file_path, _paths.SAFE_ROOT, _paths.ROOT)


def outside_root(file_path: str) -> str:
    return file_guard.outside_root_message(file_path, _paths.SAFE_ROOT, _paths.ROOT)
