"""Lane A: tools Lucy writes for herself (`user_tool` tool). Constitution file (src/lucy/selfdev/ is hash-locked).

Life of a user tool
  create  -> static check -> file written to src/lucy/tools_user/<name>.py (+ <name>_test.py) -> its tests run in the
             sandbox -> on success it is registered in registry.default (hot: no restart) with status "probation".
  use     -> every call runs in the sandbox (sandbox.run_job), never inside Lucy. N good calls end probation;
             FAILS_TO_DISABLE failures in a row disable it automatically (improve re-enables it).
  improve -> new code (+ tests) -> same checks -> new version; the old version stays active if the new one fails.

What the agent can NOT do (enforced here, not by asking the model):
  * choose the tier: user tools run at tier "exec" (asks in the default mode) until the USER promotes them in
    Settings > Evolve. The tier lives in runtime/selfdev/user_tools.json, which only this code writes (the safety layer
    refuses agent writes to runtime/selfdev). The SCHEMA/TIER lines in the tool file are informational only.
  * run a file that changed behind its back: each run compares the file hash with the one recorded at test time.
  * re-enable a tool the user switched off, exceed the daily limit, or shadow a built-in tool name.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import sandbox

NAME_RX = re.compile(r"^[a-z][a-z0-9_]{2,39}$")
PROBATION_USES = int(os.environ.get("LUCY_TOOL_PROBATION", "10") or 10)
FAILS_TO_DISABLE = 3
MAX_TOOLS = 40
MAX_CODE = 16000
MAX_TESTS = 8000
DAILY_LIMIT = 12                    # creates + improves per day (agent)
DEFAULT_TIER = "exec"
CALL_TIMEOUT = 30.0
TEST_TIMEOUT = 60.0
RESERVED = {"tool_search", "update_plan", "finish_task", "user_tool", "core_change"}
PATH_ARG_NAMES = ("file_path", "dir_path", "path", "folder", "directory", "source", "destination", "dest", "target")
_TYPES = {"string", "integer", "number", "boolean", "array", "object"}

HEADER = '''"""User tool `{name}` (written by Lucy with user_tool, version {version}). Do not edit by hand: change it with
user_tool action=improve, otherwise the harness disables it (its hash is checked before every run).
Reason: {reason}
"""
# Informational only: the real tier is the user's choice (Settings > Evolve), stored outside this file.
SCHEMA = {schema!r}
TIER = {tier!r}
LABEL = "run"
NEEDS = {needs!r}


def handler(args, ctx=None):
    return run(args)


# ---- tool code ------------------------------------------------------------------------------------------------
'''


class ToolError(ValueError):
    """A create / improve / disable request that cannot be done; the message tells the model how to fix it."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _two_sentences(text: str, limit: int = 240) -> str:
    text = " ".join(str(text or "").split())
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = " ".join(parts[:2]).strip()
    return out[:limit].rstrip()


def build_parameters(params: Any) -> dict:
    """Forgiving: {"text": "string: what to reverse"} or {"text": {"type": "string", "description": ...}}, or a list
    of names. Every parameter is required unless its description starts with 'optional'."""
    props: Dict[str, dict] = {}
    required: List[str] = []
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except ValueError:
            params = [p.strip() for p in params.split(",") if p.strip()]
    if isinstance(params, list):
        params = {str(p): "string" for p in params}
    if not isinstance(params, dict):
        params = {}
    if params.get("type") == "object" and isinstance(params.get("properties"), dict):       # already a JSON schema
        req = [r for r in params.get("required") or [] if r in params["properties"]]
        params = {k: dict(v, **({} if k in req else {"optional": True})) if isinstance(v, dict) else v
                  for k, v in params["properties"].items()}
    for raw_name, spec in list(params.items())[:12]:
        name = re.sub(r"[^a-zA-Z0-9_]", "_", str(raw_name)).strip("_")[:40]
        if not name:
            continue
        if isinstance(spec, dict):
            typ = str(spec.get("type") or "string").lower()
            desc = str(spec.get("description") or "")
            optional = bool(spec.get("optional"))
            enum = spec.get("enum") if isinstance(spec.get("enum"), list) else None
        else:
            s = str(spec or "string")
            m = re.match(r"\s*(optional\s+)?(string|integer|number|boolean|array|object|int|str|bool|float|list|dict)\b"
                         r"\s*[:\-]?\s*(.*)", s, re.I)
            optional = bool(m and m.group(1)) or s.strip().lower().startswith("optional")
            typ = (m.group(2).lower() if m else "string")
            desc = (m.group(3) if m else s).strip()
            enum = None
        typ = {"int": "integer", "str": "string", "bool": "boolean", "float": "number", "list": "array",
               "dict": "object"}.get(typ, typ)
        if typ not in _TYPES:
            typ = "string"
        if desc.lower().startswith("optional"):
            optional, desc = True, desc[8:].lstrip(" :-,")
        p: Dict[str, Any] = {"type": typ}
        if desc:
            p["description"] = _two_sentences(desc, 160)
        if enum:
            p["enum"] = [str(e) for e in enum][:12]
        if typ == "array":
            p["items"] = {"type": "string"}
        props[name] = p
        if not optional:
            required.append(name)
    return {"type": "object", "properties": props, "required": required}


class UserTools:
    def __init__(self, *, tools_dir: Path, state_dir: Path, registry=None, clock: Callable[[], float] = time.time,
                 deny_read: Optional[Callable[[], List[str]]] = None, resolve: Optional[Callable[[str], Optional[str]]] = None,
                 python: Optional[str] = None):
        self.tools_dir, self.state_dir = Path(tools_dir), Path(state_dir)
        self.state_path = self.state_dir / "user_tools.json"
        self.log_path = self.state_dir / "user_tools_log.jsonl"
        self.versions_dir = self.state_dir / "user_tool_versions"
        self.registry = registry
        self.clock = clock
        self._deny_read = deny_read or (lambda: [])
        self._resolve = resolve or (lambda p: p)
        self.python = python
        self._lock = threading.RLock()

    # ============================================================================== state
    def _load(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("tools"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"tools": {}, "daily": {}}

    def _save(self, data: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _log(self, **kw) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        kw = {"ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.clock())), **kw}
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(kw, ensure_ascii=False) + "\n")

    def log(self, limit: int = 60) -> List[dict]:
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            return []
        out = []
        for ln in reversed(lines):
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
        return out

    def _day(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self.clock()))

    def files(self, name: str):
        return self.tools_dir / f"{name}.py", self.tools_dir / f"{name}_test.py"

    # ============================================================================== agent actions
    def create(self, *, name: str, description: str, code: str, tests: str, parameters: Any = None,
               needs: Any = None, reason: str = "", session: Optional[str] = None, improve: bool = False) -> dict:
        with self._lock:
            data = self._load()
            name = str(name or "").strip().lower()
            cur = data["tools"].get(name)
            if improve:
                if cur is None:
                    raise ToolError(f"there is no user tool called {name!r}; use action=create")
                if cur.get("disabled_by") == "user":
                    raise ToolError(f"the user switched {name} off; only the user can switch it on again (Settings > Evolve)")
            else:
                if cur is not None:
                    raise ToolError(f"{name} already exists; use action=improve to change it")
                if not NAME_RX.match(name) or "__" in name:
                    raise ToolError("name must be 3-40 characters: lowercase letters, digits and _ (start with a letter)")
                if name in RESERVED or (self.registry is not None and self.registry.get(name) is not None):
                    raise ToolError(f"{name} is already a built-in tool name; pick another name")
                if len(data["tools"]) >= MAX_TOOLS:
                    raise ToolError(f"there are already {MAX_TOOLS} user tools; disable or improve one instead")
            day = self._day()
            used = int((data.get("daily") or {}).get(day, 0))
            if used >= DAILY_LIMIT:
                raise ToolError(f"daily limit reached ({DAILY_LIMIT} tool changes per day); try again tomorrow")
            if not str(reason or "").strip():
                raise ToolError("give a reason (why this tool is needed, in a few words)")
            code = str(code or "").replace("\r\n", "\n")
            if not code.strip():
                raise ToolError("code is empty: write a top-level  def run(args):  that returns a string")
            if len(code) > MAX_CODE:
                raise ToolError(f"code is too long ({len(code)} characters, max {MAX_CODE}); make the tool smaller")
            tests = str(tests if tests not in (None, "") else (self._read_tests(name) if improve else "")).replace("\r\n", "\n")
            if not tests.strip():
                raise ToolError("tests are required: plain asserts that call run(...), e.g.  assert run({'x': 2}) == '4'")
            if len(tests) > MAX_TESTS:
                raise ToolError(f"tests are too long (max {MAX_TESTS} characters)")
            if needs in (None, "") and improve:
                needs = cur.get("needs", [])
            needs = self._needs(needs)
            description = _two_sentences(description) if str(description or "").strip() else (cur or {}).get("description", "")
            if not description:
                raise ToolError("description is required (one or two short sentences: what the tool does)")
            if parameters in (None, "", {}) and improve:
                params = cur.get("parameters") or {"type": "object", "properties": {}, "required": []}
            else:
                params = build_parameters(parameters)
            problems = sandbox.static_check(code, needs) + [f"tests: {p}" for p in sandbox.static_check(tests, needs, is_tests=True)]
            if problems:
                self._log(event="rejected", tool=name, stage="static_check", problems=problems, session=session)
                raise ToolError("not saved, fix these first: " + "; ".join(problems))
            version = int((cur or {}).get("version", 0)) + 1
            tier = (cur or {}).get("tier") or max(DEFAULT_TIER, sandbox.tier_floor(needs), key=sandbox.TIER_ORDER.index)
            schema = {"type": "function", "function": {"name": name, "description": description, "parameters": params}}
            text = HEADER.format(name=name, version=version, reason=_two_sentences(reason, 200).replace('"""', "'''"),
                                 schema=schema, tier=tier, needs=needs) + code.rstrip() + "\n"
            # Test a staged copy first: a failing improve must not replace the working version.
            self.tools_dir.mkdir(parents=True, exist_ok=True)
            stage = self.state_dir / "staging" / f"{name}.py"
            stage.parent.mkdir(parents=True, exist_ok=True)
            stage.write_text(text, encoding="utf-8")
            res = sandbox.run_job(stage, mode="test", tests=tests, timeout=TEST_TIMEOUT,
                                  deny_read=self._deny_read(), python=self.python)
            if not res.ok:
                self._log(event="rejected", tool=name, stage="tests", version=version, error=res.error[:400], session=session)
                stage.unlink(missing_ok=True)
                kept = f" The working version {cur['version']} stays active." if improve else ""
                raise ToolError(f"tests failed, nothing changed: {res.error[:700]}.{kept} Fix the code or the tests and try again.")
            mod_path, test_path = self.files(name)
            self._write_atomic(mod_path, text)
            self._write_atomic(test_path, tests if tests.endswith("\n") else tests + "\n")
            stage.unlink(missing_ok=True)
            vdir = self.versions_dir / name
            vdir.mkdir(parents=True, exist_ok=True)
            (vdir / f"v{version}.py").write_text(text, encoding="utf-8")
            (vdir / f"v{version}_test.py").write_text(tests, encoding="utf-8")
            now = self.clock()
            entry = dict(cur or {}, name=name, description=description, parameters=params, needs=needs, tier=tier,
                         version=version, sha=_sha(text), tests_sha=_sha(tests), status="probation", enabled=True,
                         disabled_by=None, disabled_reason="", uses_ok=0, fails_in_row=0,
                         probation_left=PROBATION_USES, updated=now)
            entry.setdefault("created", now)
            entry.setdefault("uses_total", 0)
            entry.setdefault("fails_total", 0)
            prov = list(entry.get("history") or [])
            prov.append({"version": version, "ts": now, "session": session, "reason": _two_sentences(reason, 300),
                         "by": "agent", "tests": res.text[:120]})
            entry["history"] = prov[-20:]
            data["tools"][name] = entry
            data.setdefault("daily", {})[day] = used + 1
            data["daily"] = {k: v for k, v in data["daily"].items() if k >= time.strftime("%Y-%m-%d", time.localtime(now - 8 * 86400))}
            self._save(data)
            self._log(event="improved" if improve else "created", tool=name, version=version, needs=needs, tier=tier,
                      reason=_two_sentences(reason, 300), session=session)
            self.sync()
            return entry

    def disable(self, name: str, reason: str = "", by: str = "agent", session: Optional[str] = None) -> dict:
        with self._lock:
            data = self._load()
            t = data["tools"].get(str(name or "").strip().lower())
            if t is None:
                raise ToolError(f"there is no user tool called {name!r}")
            t.update(enabled=False, disabled_by=by, disabled_reason=_two_sentences(reason or f"switched off by {by}", 200),
                     status="disabled")
            self._save(data)
            self._log(event="disabled", tool=t["name"], by=by, reason=t["disabled_reason"], session=session)
            self.sync()
            return t

    def list(self) -> List[dict]:
        data = self._load()
        out = []
        for t in sorted(data["tools"].values(), key=lambda x: x["name"]):
            out.append({k: t.get(k) for k in ("name", "description", "status", "enabled", "tier", "needs", "version",
                                              "uses_total", "fails_total", "fails_in_row", "probation_left",
                                              "disabled_by", "disabled_reason", "created", "updated")}
                       | {"tier_floor": sandbox.tier_floor(t.get("needs") or ()),
                          "reason": ((t.get("history") or [{}])[-1]).get("reason", ""),
                          "session": ((t.get("history") or [{}])[-1]).get("session")})
        return out

    # ============================================================================== user actions (API / UI only)
    def enable(self, name: str, by: str = "user") -> dict:
        with self._lock:
            data = self._load()
            t = data["tools"].get(name)
            if t is None:
                raise ToolError(f"no user tool {name!r}")
            t.update(enabled=True, disabled_by=None, disabled_reason="", fails_in_row=0,
                     status="probation" if t.get("probation_left", 0) > 0 else "active")
            self._save(data)
            self._log(event="enabled", tool=name, by=by)
            self.sync()
            return t

    def set_tier(self, name: str, tier: str, by: str = "user") -> dict:
        """Promote / demote. Never below what the tool's needs require (a 'files' tool cannot become 'read')."""
        with self._lock:
            data = self._load()
            t = data["tools"].get(name)
            if t is None:
                raise ToolError(f"no user tool {name!r}")
            if tier not in sandbox.TIER_ORDER:
                raise ToolError(f"tier must be one of {sandbox.TIER_ORDER}")
            floor = sandbox.tier_floor(t.get("needs") or ())
            if sandbox.TIER_ORDER.index(tier) < sandbox.TIER_ORDER.index(floor):
                raise ToolError(f"{name} needs {t.get('needs')}, so its tier cannot be lower than {floor}")
            old = t.get("tier")
            t["tier"] = tier
            self._save(data)
            self._log(event="tier", tool=name, by=by, old=old, new=tier)
            self.sync()
            return t

    def delete(self, name: str, by: str = "user") -> None:
        with self._lock:
            data = self._load()
            if data["tools"].pop(name, None) is None:
                raise ToolError(f"no user tool {name!r}")
            self._save(data)
            for p in self.files(name):
                p.unlink(missing_ok=True)          # older versions stay in runtime/selfdev/user_tool_versions/
            self._log(event="deleted", tool=name, by=by)
            self.sync()

    def revert(self, name: str, by: str = "user") -> dict:
        """Go back to the previous version (its files are kept in runtime/selfdev/user_tool_versions/<name>/)."""
        with self._lock:
            data = self._load()
            t = data["tools"].get(name)
            if t is None:
                raise ToolError(f"no user tool {name!r}")
            prev = int(t.get("version", 1)) - 1
            vdir = self.versions_dir / name
            src, srct = vdir / f"v{prev}.py", vdir / f"v{prev}_test.py"
            if prev < 1 or not src.exists():
                raise ToolError(f"{name} has no earlier version")
            text = src.read_text(encoding="utf-8")
            tests = srct.read_text(encoding="utf-8") if srct.exists() else ""
            mod_path, test_path = self.files(name)
            self._write_atomic(mod_path, text)
            self._write_atomic(test_path, tests)
            t.update(version=prev, sha=_sha(text), tests_sha=_sha(tests), fails_in_row=0, status="probation",
                     probation_left=PROBATION_USES, enabled=t.get("disabled_by") != "user")
            self._save(data)
            self._log(event="reverted", tool=name, by=by, version=prev)
            self.sync()
            return t

    # ============================================================================== running
    def _read_tests(self, name: str) -> str:
        try:
            return self.files(name)[1].read_text(encoding="utf-8")
        except OSError:
            return ""

    @staticmethod
    def _needs(needs: Any) -> List[str]:
        if isinstance(needs, str):
            needs = [n.strip() for n in re.split(r"[,\s]+", needs) if n.strip()]
        out = []
        for n in needs or ():
            n = str(n).lower().strip()
            n = {"file": "files", "program": "programs", "web": "internet", "network": "internet", "net": "internet",
                 "commands": "programs", "shell": "programs", "registry": "system"}.get(n, n)
            if n in ("", "none", "nothing"):
                continue
            if n not in sandbox.NEEDS:
                raise ToolError(f"needs may only contain {', '.join(sandbox.NEEDS)} (or be empty); got {n!r}")
            if n not in out:
                out.append(n)
        return out

    @staticmethod
    def _write_atomic(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)

    def _record(self, name: str, ok: bool, error: str = "") -> dict:
        with self._lock:
            data = self._load()
            t = data["tools"].get(name)
            if t is None:
                return {}
            t["uses_total"] = int(t.get("uses_total", 0)) + 1
            if ok:
                t["fails_in_row"] = 0
                t["uses_ok"] = int(t.get("uses_ok", 0)) + 1
                if t.get("status") == "probation":
                    t["probation_left"] = max(0, int(t.get("probation_left", 0)) - 1)
                    if t["probation_left"] == 0:
                        t["status"] = "active"
                        self._log(event="probation_passed", tool=name)
            else:
                t["fails_in_row"] = int(t.get("fails_in_row", 0)) + 1
                t["fails_total"] = int(t.get("fails_total", 0)) + 1
                t["last_error"] = error[:300]
                if t["fails_in_row"] >= FAILS_TO_DISABLE and t.get("enabled"):
                    t.update(enabled=False, disabled_by="auto", status="disabled",
                             disabled_reason=f"failed {t['fails_in_row']} times in a row: {error[:160]}")
                    self._log(event="auto_disabled", tool=name, error=error[:300])
            self._save(data)
            if not t.get("enabled"):
                self.sync()
            return t

    def _write_paths(self, args: dict) -> List[str]:
        out = []
        for k in PATH_ARG_NAMES:
            v = (args or {}).get(k)
            if isinstance(v, str) and v.strip():
                r = self._resolve(v)
                if r:
                    out.append(r)
        return out

    def call(self, name: str, args: dict) -> str:
        data = self._load()
        t = data["tools"].get(name)
        if t is None or not t.get("enabled"):
            why = (t or {}).get("disabled_reason") or "it does not exist"
            return f"Error: user tool {name} is not available ({why})."
        mod_path = self.files(name)[0]
        try:
            text = mod_path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if _sha(text) != t.get("sha"):
            self.disable(name, "its file changed outside user_tool (not tested); improve it to re-enable", by="auto")
            return (f"Error: {name} was changed outside user_tool, so it was switched off and not run. "
                    "Use user_tool action=improve with the code you want (it re-runs the tests).")
        res = sandbox.run_job(mod_path, mode="call", args=args, caps=t.get("needs") or (),
                              write_paths=self._write_paths(args), deny_read=self._deny_read(),
                              timeout=CALL_TIMEOUT, python=self.python)
        if not res.ok:
            after = self._record(name, False, res.error)
            off = "" if after.get("enabled", True) else " It is now switched OFF after repeated failures; improve it to fix."
            hint = " The sandbox blocked it: the tool tried something its needs do not allow." if res.blocked else ""
            return f"Error: {name} failed: {res.error[:800]}.{hint}{off}"
        after = self._record(name, True)
        out = res.text if res.text.strip() else "(the tool returned nothing)"
        if after.get("status") == "probation":
            out += f"\n[user tool on probation: {after.get('probation_left', 0)} more good runs; check this result]"
        return out

    # ============================================================================== registry
    def specs(self) -> List[dict]:
        return [t for t in self._load()["tools"].values() if t.get("enabled")]

    def schemas(self) -> List[dict]:
        return [{"type": "function", "function": {"name": t["name"], "description": t["description"] + " (user tool)",
                                                   "parameters": t.get("parameters") or {"type": "object", "properties": {}}}}
                for t in self.specs()]

    def keywords(self) -> Dict[str, str]:
        return {t["name"]: "my own tool user tool custom " + t["name"].replace("_", " ") for t in self.specs()}

    def sync(self) -> int:
        """Make registry.default match the enabled user tools (hot reload: called after every change)."""
        if self.registry is None:
            return 0
        from lucy.tools.registry import ToolSpec
        wanted = {t["name"]: t for t in self.specs()}
        for spec in list(self.registry.all_specs()):
            if spec.module.startswith("lucy.tools_user.") and spec.name not in wanted:
                self.registry.unregister(spec.name)
        n = 0
        for name, t in wanted.items():
            existing = self.registry.get(name)
            if existing is not None and not existing.module.startswith("lucy.tools_user."):
                continue                                     # never shadow a built-in / MCP tool
            schema = {"type": "function", "function": {"name": name, "description": t["description"] + " (user tool)",
                                                       "parameters": t.get("parameters") or {"type": "object", "properties": {}}}}
            if existing is not None:
                self.registry.unregister(name)
            self.registry.register(ToolSpec(name=name, schema=schema, handler=self._handler(name), tier=t["tier"],
                                            label="run", module=f"lucy.tools_user.{name}"))
            n += 1
        return n

    def _handler(self, name: str):
        def handler(args, ctx):
            return self.call(name, args or {})
        return handler
