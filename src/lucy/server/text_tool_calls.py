"""Recover tool calls the model wrote as TEXT instead of as a structured `tool_calls` message.

llama-server only returns structured tool calls when the model's output matches its chat template's
tool format. When it doesn't (high temperature, template without tool support, ...) the model just
prints something like

    Action: web_search(query="latest GPU news")

and, without this module, nothing runs. `TextToolCallFilter` sits on the content stream:

  * feed()   returns the text that is safe to show; anything that looks like a text tool call is
             held back so the user never sees "Action: web_search(...)" in the chat
  * finish() flushes what is left
  * calls()  parses the held-back text into tool calls (only for tool names that really exist)
  * If parsing fails, `held_text` lets the caller show the text after all (nothing is lost).

Recognised formats
  Action: tool(query="x", limit=3)          Action: tool({"query": "x"})
  Action: tool \\n Action Input: {"query": "x"}
  <tool_call>{"name": "tool", "arguments": {...}}</tool_call>
  [TOOL_CALLS][{"name": "tool", "arguments": {...}}]            (Mistral)
  <function=tool><parameter=query>x</parameter></function>      (Qwen-coder style)
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

_FIXED_MARKERS = ("<tool_call>", "<function=", "[TOOL_CALLS]")
_SNAKE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")
_HOLD_WINDOW = 80          # how far back an undecided marker can start


class TextToolCallFilter:
    def __init__(self, tools: list[dict]):
        self.schemas: dict[str, dict] = {}
        for t in tools or []:
            fn = t.get("function", {})
            if fn.get("name"):
                self.schemas[fn["name"]] = fn.get("parameters", {}) or {}
        names = sorted(self.schemas, key=len, reverse=True)
        alt = "|".join(re.escape(n) for n in names) or r"(?!)"
        # "Action: tool(...)", also wrapped in markdown ("**Action:** `tool(...)`"). A snake_case name followed
        # by "(" counts even if no such tool exists, so the loop can answer "unknown tool" instead of the
        # model believing it did something.
        # A line that STARTS with a real tool name + ( or { is also a call attempt:  send_sticker{"emotion": "Happy"}
        # A snake_case tool name with a bracket attached is a call anywhere in the text ("Sure, send_sticker{...}").
        us = "|".join(re.escape(n) for n in sorted((n for n in self.schemas if "_" in n), key=len, reverse=True)) or r"(?!)"
        self._bare = re.compile(r"(?<![^\n])[ \t]*[*_`]{0,4}(" + alt + r")(?=[({])|(?<![\w`*])(" + us + r")(?=[({])")
        self._start = re.compile(
            r"[*_`]{0,4}[ \t]*Action[ \t]*[*_`]{0,4}[ \t]*:[*_`\s]*(?:(?:" + alt + r")\b|"
            r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?=[ \t]*\())|<tool_call>|<function=|\[TOOL_CALLS\]"
            r"|(?<![^\n])[ \t]*[*_`]{0,4}(?:" + alt + r")(?=[({])|(?<![\w`*])(?:" + us + r")(?=[({])")
        self._action_prefix = re.compile(r"Action[ \t]*[*_`]{0,4}[ \t]*(?::[*_`\s]*([A-Za-z0-9_]*))?$")
        self.buf = ""
        self.emitted = 0
        self.tripped = False
        self.trip_at = 0

    # -- streaming ---------------------------------------------------------------
    def _undecided(self, suffix: str, bol: bool = False) -> bool:
        """Could `suffix` (the tail of the text so far) still turn into a tool-call marker?"""
        s = suffix.lstrip("*_` \t")
        if not s:
            return any(c in "*_`" for c in suffix)           # only markup so far: may precede "Action"
        if any(m.startswith(s) for m in ("Action",) + _FIXED_MARKERS):
            return True
        if (bol or len(s) >= 3) and any(n.startswith(s) for n in self.schemas):
            return True                                        # may be the start of a bare tool call
        m = self._action_prefix.match(s)
        if not m:
            return False
        name = m.group(1)
        return name is None or any(n.startswith(name) for n in self.schemas) or bool(re.fullmatch(r"[a-z][a-z0-9_]*", name or "a"))

    def feed(self, chunk: str) -> str:
        if self.tripped:
            self.buf += chunk
            return ""
        self.buf += chunk
        m = self._start.search(self.buf, self.emitted)
        if m:
            self.tripped, self.trip_at = True, m.start()
            out = self.buf[self.emitted:self.trip_at]
            self.emitted = len(self.buf)
            return out
        safe_end = len(self.buf)
        for j in range(max(self.emitted, len(self.buf) - _HOLD_WINDOW), len(self.buf)):
            if self._undecided(self.buf[j:], bol=(j == 0 or not (self.buf[j - 1].isalnum() or self.buf[j - 1] == "_"))):
                safe_end = j
                break
        out = self.buf[self.emitted:safe_end]
        self.emitted = safe_end
        return out

    def finish(self) -> str:
        """End of stream: flush held-back text unless a tool call was detected."""
        if self.tripped:
            return ""
        out = self.buf[self.emitted:]
        self.emitted = len(self.buf)
        return out

    @property
    def visible_text(self) -> str:
        """Everything before the tool call (the part the user already saw)."""
        return self.buf[:self.trip_at] if self.tripped else self.buf

    @property
    def held_text(self) -> str:
        return self.buf[self.trip_at:] if self.tripped else ""

    # -- parsing -----------------------------------------------------------------
    def calls(self) -> list[dict]:
        """Parse held-back text into [{'id','name','arguments'(json str)}]; [] if it can't be parsed."""
        if not self.tripped:
            return []
        text = self.held_text
        found: list[tuple[str, dict]] = []
        try:
            found += self._parse_json_blocks(text)
            found += self._parse_xml_function(text)
            found += self._parse_action(text)
            found += self._parse_bare(text)
        except Exception:                      # never let a parsing bug break the chat
            return []
        out, seen = [], set()
        for i, (name, args) in enumerate(found):
            if name not in self.schemas and not _SNAKE.fullmatch(name or ""):
                continue                                     # unknown names are passed on only if tool-like
            key = (name, json.dumps(args, sort_keys=True, default=str))
            if key in seen:
                continue
            seen.add(key)
            out.append({"id": f"call_text_{i}", "name": name,
                        "arguments": json.dumps(args, ensure_ascii=False)})
        return out

    # <tool_call>{...}</tool_call>   and   [TOOL_CALLS][{...}]
    def _parse_json_blocks(self, text: str) -> list[tuple[str, dict]]:
        dec = json.JSONDecoder(strict=False)
        res = []
        starts = [m.end() for m in re.finditer(r"<tool_call>|\[TOOL_CALLS\]", text)]
        for pos in starts:
            while pos < len(text) and text[pos] in " \t\r\n":
                pos += 1
            try:
                obj, _ = dec.raw_decode(text, pos)
            except ValueError:
                continue
            for item in (obj if isinstance(obj, list) else [obj]):
                if not isinstance(item, dict):
                    continue
                fn = item.get("function", item)
                name = fn.get("name")
                args = fn.get("arguments", fn.get("parameters", {}))
                if isinstance(args, str):
                    args = self._load_obj(args) or {}
                if name:
                    res.append((name, args if isinstance(args, dict) else {}))
        return res

    # <function=name><parameter=key>value</parameter></function>
    def _parse_xml_function(self, text: str) -> list[tuple[str, dict]]:
        res = []
        for m in re.finditer(r"<function=([\w.\-]+)>(.*?)(?:</function>|$)", text, re.DOTALL):
            args = {}
            for p in re.finditer(r"<parameter=([\w.\-]+)>\n?(.*?)\n?</parameter>", m.group(2), re.DOTALL):
                args[p.group(1)] = self._coerce(p.group(1), p.group(2), m.group(1))
            res.append((m.group(1), args))
        return res

    # Action: name(...)   /   Action: name + Action Input: {...}
    def _parse_action(self, text: str) -> list[tuple[str, dict]]:
        res = []
        for m in self._start.finditer(text):
            if "Action" not in m.group(0):
                continue
            name = re.search(r"(\w+)\s*$", m.group(0)).group(1)
            pos = m.end()
            rest = text[pos:]
            stripped = rest.lstrip(" \t")
            if stripped.startswith("("):
                inner = self._balanced(stripped)
                if inner is None:
                    continue                    # truncated: can't trust partial arguments
                res.append((name, self._parse_args(inner, name)))
                continue
            mi = re.match(r"\s*\n\s*Action Input:\s*", rest, re.IGNORECASE)
            if mi:
                payload = rest[mi.end():]
                obj = self._load_obj(payload.lstrip()) if payload.lstrip().startswith("{") else None
                if obj is None:
                    obj = self._parse_args(payload.split("\n", 1)[0].strip(), name)
                res.append((name, obj))
            else:
                res.append((name, {}))
        return res

    # name(...)  /  name{...}  at the start of a line, for tools that exist
    def _parse_bare(self, text: str) -> list[tuple[str, dict]]:
        res = []
        for m in self._bare.finditer(text):
            name, pos = m.group(1) or m.group(2), m.end()
            inner = self._balanced(text[pos:])
            if inner is None:
                continue                                       # truncated: don't guess
            if text[pos] == "{":
                args = self._load_obj("{" + inner + "}") or self._parse_args(inner, name)
            else:
                args = self._parse_args(inner, name)
            res.append((name, args))
        return res

    # -- helpers ------------------------------------------------------------------
    @staticmethod
    def _balanced(s: str) -> str | None:
        """s starts with '('. Return the text inside the matching ')' (string-aware) or None."""
        depth, i, quote = 0, 0, None
        while i < len(s):
            ch = s[i]
            if quote:
                if s.startswith(quote, i) and (len(quote) == 3 or s[i - 1] != "\\" or s[i - 2:i] == "\\\\"):
                    i += len(quote) - 1
                    quote = None
                elif ch == "\\":
                    i += 1
            elif s.startswith('"""', i) or s.startswith("'''", i):
                quote = s[i:i + 3]
                i += 2
            elif ch in "\"'":
                quote = ch
            elif ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth == 0:
                    return s[1:i]
            i += 1
        return None

    @staticmethod
    def _load_obj(s: str) -> dict | None:
        try:
            obj, _ = json.JSONDecoder(strict=False).raw_decode(s)
            return obj if isinstance(obj, dict) else None
        except ValueError:
            pass
        try:
            obj = ast.literal_eval(s.strip())
            return obj if isinstance(obj, dict) else None
        except (ValueError, SyntaxError):
            return None

    def _first_param(self, tool: str) -> str | None:
        schema = self.schemas.get(tool, {})
        req = schema.get("required") or []
        props = list((schema.get("properties") or {}).keys())
        return (req or props or [None])[0]

    def _coerce(self, key: str, value: str, tool: str) -> Any:
        typ = ((self.schemas.get(tool, {}).get("properties") or {}).get(key) or {}).get("type")
        if typ in ("integer", "number"):
            try:
                return int(value) if typ == "integer" else float(value)
            except ValueError:
                return value
        if typ == "boolean":
            return value.strip().lower() in ("true", "1", "yes")
        return value

    def _parse_args(self, inner: str, tool: str) -> dict:
        inner = inner.strip()
        if not inner:
            return {}
        if inner.startswith("{"):
            obj = self._load_obj(inner)
            if obj is not None:
                return obj
        try:                                                    # query="x", limit=3  /  "x"
            call = ast.parse(f"_f({inner})", mode="eval").body
            args = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords if kw.arg}
            pos = [ast.literal_eval(a) for a in call.args]
            if pos and (first := self._first_param(tool)):
                args.setdefault(first, pos[0])
            return args
        except (SyntaxError, ValueError):
            pass
        args = {}                                               # query: "x", limit: 3
        for m in re.finditer(r"(\w+)\s*[:=]\s*(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^,]+)", inner):
            raw = m.group(2).strip()
            try:
                args[m.group(1)] = ast.literal_eval(raw)
            except (ValueError, SyntaxError):
                args[m.group(1)] = raw.strip("\"'")
        if not args and (first := self._first_param(tool)):
            args[first] = inner.strip("\"'")                    # bare text: Action: web_search(cats)
        return args
