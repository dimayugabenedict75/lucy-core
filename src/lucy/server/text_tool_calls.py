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
        self._start = re.compile(
            r"Action:\s*[`*]*\s*(?:" + alt + r")\b|<tool_call>|<function=|\[TOOL_CALLS\]")
        self._action_prefix = re.compile(r"Action:\s*[`*]*\s*(\w*)$")
        self.buf = ""
        self.emitted = 0
        self.tripped = False
        self.trip_at = 0

    # -- streaming ---------------------------------------------------------------
    def _undecided(self, suffix: str) -> bool:
        if any(m.startswith(suffix) for m in ("Action:",) + _FIXED_MARKERS):
            return True
        m = self._action_prefix.match(suffix)
        return bool(m and any(n.startswith(m.group(1)) for n in self.schemas))

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
            if self._undecided(self.buf[j:]):
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
        except Exception:                      # never let a parsing bug break the chat
            return []
        out, seen = [], set()
        for i, (name, args) in enumerate(found):
            if name not in self.schemas:
                continue
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
            if not m.group(0).startswith("Action:"):
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
