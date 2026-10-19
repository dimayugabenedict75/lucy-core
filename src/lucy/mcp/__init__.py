"""MCP (Model Context Protocol) client for Lucy Core (P4).

    from lucy import mcp
    mgr = mcp.get()          # process-wide McpManager (api.py calls mgr.start() at startup)

Servers are declared in `.core/mcp.json` (a CONSTITUTION file: only the user can change it, then reseal). Their tools
are registered in the tool registry as `server__tool`, but never put in the prompt: the model finds them with
`tool_search` (or calls them by name) like any other hidden tool.
"""
from __future__ import annotations

from typing import Optional

from .client import McpError  # noqa: F401
from .manager import McpManager

_default: Optional[McpManager] = None


def get() -> McpManager:
    global _default
    if _default is None:
        from lucy import paths as _paths
        _default = McpManager(root=_paths.ROOT, runtime=_paths.RUNTIME)
    return _default


def set_default(mgr: Optional[McpManager]) -> None:
    """Tests install a manager on temporary folders (None = rebuild from lucy.paths on next get())."""
    global _default
    if _default is not None and _default is not mgr:
        try:
            _default.shutdown()
        except Exception:                                   # noqa: BLE001
            pass
    _default = mgr
