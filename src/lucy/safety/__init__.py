"""Lucy's safety layer ("constitution"): permissions, approvals, audit log, undo, prompt-injection guard.

This package is a constitution file set: the agent may read it but can never change it (see constitution.py).

    from lucy import safety
    layer = safety.get()        # the process-wide SafetyLayer (api.py calls safety.install() at startup)
"""
from __future__ import annotations

from typing import Optional

from .config import MODE_LABELS, MODES  # noqa: F401
from .layer import CURRENT, SafetyLayer  # noqa: F401
from .policy import ASK, ASK_HARD, AUTO, DENY, Decision  # noqa: F401

_default: Optional[SafetyLayer] = None


def get() -> SafetyLayer:
    global _default
    if _default is None:
        from lucy import paths as _paths
        _default = SafetyLayer(root=_paths.ROOT, runtime=_paths.RUNTIME, safe_root=_paths.SAFE_ROOT)
    return _default


def set_default(layer: Optional[SafetyLayer]) -> None:
    """Tests use this to install a layer with temporary folders (None = rebuild from lucy.paths on next get())."""
    global _default
    _default = layer


def install() -> SafetyLayer:
    """Startup (api.py): build the layer from lucy.paths and check the constitution lock."""
    layer = get()
    layer.start()
    return layer
