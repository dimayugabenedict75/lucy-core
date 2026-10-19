"""Tool registry: auto-discovers tool modules in this package so adding a tool = adding one file.

Keep it small and strict (the runtime model is 4-12B): every tool must have a schema, a handler, a tier and a
label, otherwise discovery fails loudly at startup instead of the tool misbehaving at runtime.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import pkgutil
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

VALID_TIERS = ("read", "write", "exec", "system", "network")
_SKIP = {"registry"}          # modules in the package that are never tool modules


class ToolSpecError(ValueError):
    """A tool module is incomplete or inconsistent."""


@dataclass
class ToolContext:
    """What a handler may need besides its arguments. Grows over time; handlers must not assume more."""
    session_id: Optional[str] = None
    tool_names: tuple = ()          # names of every tool in the prompt (send_tts uses it to strip tool calls from speech)


@dataclass
class ToolSpec:
    name: str
    schema: dict
    handler: Callable[..., Any]
    tier: str
    label: str
    module: str = ""

    async def run(self, args: dict, ctx: ToolContext) -> str:
        """Run the handler; sync handlers go to a worker thread so they can never block the event loop.

        EVERY tool execution passes the safety layer here (constitution: this file is hash-locked):
        before() applies the hard rules and snapshots files about to change; after() wraps untrusted content and
        writes the audit log. Asking the user happens earlier, in the agent loop (safety.SafetyLayer.gate)."""
        from lucy import safety                         # lazy: the safety layer resolves paths through lucy.tools
        layer = safety.get()
        refusal = layer.before(self.name, self.tier, args, ctx.session_id)
        if refusal is not None:
            return refusal
        t0 = time.monotonic()
        try:
            if inspect.iscoroutinefunction(self.handler):
                out = await self.handler(args, ctx)
            else:
                out = await asyncio.to_thread(self.handler, args, ctx)
        except BaseException as e:
            if not isinstance(e, asyncio.CancelledError):
                layer.after(self.name, self.tier, args, ctx.session_id, "", int((time.monotonic() - t0) * 1000),
                            error=f"{type(e).__name__}: {e}")
            raise
        out = out if isinstance(out, str) else str(out)
        return layer.after(self.name, self.tier, args, ctx.session_id, out, int((time.monotonic() - t0) * 1000))


def _make_spec(schema: Any, handler: Any, tier: Any, label: Any, module: str) -> ToolSpec:
    where = f"tool module '{module}'"
    fn = schema.get("function") if isinstance(schema, dict) else None
    if not isinstance(fn, dict) or schema.get("type") != "function":
        raise ToolSpecError(f"{where}: SCHEMA must be {{'type': 'function', 'function': {{...}}}}")
    name = fn.get("name")
    if not isinstance(name, str) or not name:
        raise ToolSpecError(f"{where}: SCHEMA.function.name is missing")
    if not fn.get("description"):
        raise ToolSpecError(f"{where} ({name}): SCHEMA.function.description is missing")
    if not isinstance(fn.get("parameters"), dict):
        raise ToolSpecError(f"{where} ({name}): SCHEMA.function.parameters is missing")
    if not callable(handler):
        raise ToolSpecError(f"{where} ({name}): handler is missing or not callable")
    if tier not in VALID_TIERS:
        raise ToolSpecError(f"{where} ({name}): TIER must be one of {VALID_TIERS}, got {tier!r}")
    if not isinstance(label, str) or not label.strip():
        raise ToolSpecError(f"{where} ({name}): LABEL is missing")
    return ToolSpec(name=name, schema=schema, handler=handler, tier=tier, label=label.strip(), module=module)


class Registry:
    def __init__(self) -> None:
        self._specs: Dict[str, ToolSpec] = {}

    # -- registration ------------------------------------------------------------------------------
    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ToolSpecError(f"tool '{spec.name}' is defined twice ({self._specs[spec.name].module} and {spec.module})")
        self._specs[spec.name] = spec

    def unregister(self, name: str) -> None:
        self._specs.pop(name, None)

    def add_module(self, mod: Any) -> int:
        """Register the tool(s) a module exports. Returns how many; 0 for helper modules."""
        modname = getattr(mod, "__name__", "?")
        if hasattr(mod, "TOOLS"):
            items = list(getattr(mod, "TOOLS"))
            specs = []
            for it in items:
                if not isinstance(it, dict):
                    raise ToolSpecError(f"tool module '{modname}': TOOLS entries must be dicts")
                specs.append(_make_spec(it.get("schema"), it.get("handler"), it.get("tier"), it.get("label"), modname))
        elif hasattr(mod, "SCHEMA") or hasattr(mod, "handler"):
            specs = [_make_spec(getattr(mod, "SCHEMA", None), getattr(mod, "handler", None),
                                getattr(mod, "TIER", None), getattr(mod, "LABEL", None), modname)]
        else:
            return 0
        for s in specs:
            self.register(s)
        return len(specs)

    def discover(self, package_name: str = "lucy.tools") -> int:
        """Import every module of the package and register what it exports. Safe to call twice."""
        pkg = importlib.import_module(package_name)
        n = 0
        for info in pkgutil.iter_modules(pkg.__path__):
            if info.name.startswith("_") or info.name in _SKIP:
                continue
            mod = importlib.import_module(f"{package_name}.{info.name}")
            if any(s.module == mod.__name__ for s in self._specs.values()):
                continue                      # already registered
            n += self.add_module(mod)
        return n

    # -- lookup --------------------------------------------------------------------------------------
    def get(self, name: str) -> Optional[ToolSpec]:
        return self._specs.get(name)

    def all_specs(self) -> List[ToolSpec]:
        return list(self._specs.values())

    def schemas(self) -> List[dict]:
        return [s.schema for s in self._specs.values()]

    async def run(self, name: str, args: dict, ctx: Optional[ToolContext] = None) -> str:
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(name)
        return await spec.run(args, ctx or ToolContext())


default = Registry()
