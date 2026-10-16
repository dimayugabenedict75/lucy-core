"""Lucy tools package.

Drop ONE file in this folder to add a tool. A tool module exports:

    SCHEMA   OpenAI function definition: {"type": "function", "function": {name, description, parameters}}
    handler  def handler(args: dict, ctx: ToolContext) -> str      (sync or async)
    TIER     "read" | "write" | "exec" | "system" | "network"
    LABEL    short activity word the UI already knows ("search", "read", "edit", "run", ...)

A small family of related tools can instead export `TOOLS = [{"schema":..., "handler":..., "tier":..., "label":...}, ...]`.
Modules that export neither (helpers such as google_drive.py) are ignored by discovery.
See registry.py. `Toolset` is re-exported so `from lucy.tools import Toolset` keeps working.
"""
from .google_drive import Toolset  # noqa: F401
from .registry import (  # noqa: F401
    Registry, ToolSpec, ToolContext, ToolSpecError, VALID_TIERS, default,
)
