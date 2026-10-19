"""Turns what an MCP server sends into what a 4-12B model handles well (pure functions, easy to test).

* tool names   -> `server__tool`, only [a-z0-9_-], at most 64 characters
* descriptions -> first two sentences, short
* JSON schemas -> flat and plain: no $ref / anyOf / titles / defaults, short parameter descriptions
* call results -> one plain string (text parts joined; images saved to a file and mentioned by path)
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

NAME_MAX = 64
SEP = "__"
DESC_MAX = 240
PARAM_DESC_MAX = 100
_BAD = re.compile(r"[^a-zA-Z0-9_-]+")


def tool_name(server_key: str, tool: str) -> str:
    base = _BAD.sub("_", str(tool)).strip("_") or "tool"
    full = f"{server_key}{SEP}{base}"
    if len(full) <= NAME_MAX:
        return full
    h = hashlib.sha1(str(tool).encode()).hexdigest()[:6]
    return f"{full[:NAME_MAX - 7]}_{h}"


def split_name(name: str) -> Optional[tuple]:
    """'github__create_issue' -> ('github', 'create_issue'); None when it is not an MCP-style name."""
    head, sep, tail = str(name).partition(SEP)
    return (head, tail) if sep and head and tail else None


def short_description(text: str, limit: int = DESC_MAX) -> str:
    t = " ".join(str(text or "").split())
    if not t:
        return "(no description)"
    parts = re.split(r"(?<=[.!?])\s+", t)
    out = " ".join(parts[:2])
    if len(out) > limit:
        out = out[:limit - 1].rstrip() + "\u2026"
    return out


def _non_null(options: List[Any]) -> List[dict]:
    return [o for o in options if isinstance(o, dict) and o.get("type") != "null"]


def _resolve(node: Any, defs: dict, depth: int = 0) -> Any:
    if isinstance(node, dict) and "$ref" in node and depth < 6:
        ref = str(node["$ref"]).rsplit("/", 1)[-1]
        target = defs.get(ref)
        if isinstance(target, dict):
            merged = dict(target)
            merged.update({k: v for k, v in node.items() if k != "$ref"})
            return _resolve(merged, defs, depth + 1)
        return {"type": "string"}
    return node


def _compact(node: Any, defs: dict, depth: int = 0) -> dict:
    node = _resolve(node, defs)
    if not isinstance(node, dict) or depth > 5:
        return {"type": "string"}
    for key in ("anyOf", "oneOf"):
        if isinstance(node.get(key), list):
            opts = _non_null(node[key])
            if not opts:
                return {"type": "string"}
            if all("const" in o or "enum" in o for o in opts):          # a union of literals -> one enum
                vals = []
                for o in opts:
                    vals += o.get("enum") or [o.get("const")]
                return {"type": "string", "enum": vals[:30]}
            first = dict(_resolve(opts[0], defs))
            if node.get("description") and not first.get("description"):
                first["description"] = node["description"]
            return _compact(first, defs, depth + 1)
    if isinstance(node.get("allOf"), list) and node["allOf"]:
        merged: dict = {}
        for part in node["allOf"]:
            merged.update(_resolve(part, defs))
        merged.update({k: v for k, v in node.items() if k != "allOf"})
        return _compact(merged, defs, depth + 1)
    typ = node.get("type")
    if isinstance(typ, list):
        typ = next((t for t in typ if t != "null"), "string")
    if "const" in node and "enum" not in node:
        node = dict(node, enum=[node["const"]])
    if typ is None:
        typ = "object" if "properties" in node else "array" if "items" in node else "string"
    out: Dict[str, Any] = {"type": typ}
    if node.get("description"):
        out["description"] = short_description(node["description"], PARAM_DESC_MAX)
    if isinstance(node.get("enum"), list) and node["enum"]:
        out["enum"] = node["enum"][:30]
    if typ == "object":
        props = node.get("properties") if isinstance(node.get("properties"), dict) else {}
        out["properties"] = {str(k): _compact(v, defs, depth + 1) for k, v in props.items()}
        req = [r for r in (node.get("required") or []) if r in out["properties"]]
        if req:
            out["required"] = req
    elif typ == "array":
        out["items"] = _compact(node.get("items") or {}, defs, depth + 1)
    return out


def compact_schema(schema: Any) -> dict:
    """An MCP inputSchema -> a plain OpenAI-style parameters object."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    defs = {}
    for k in ("$defs", "definitions"):
        if isinstance(schema.get(k), dict):
            defs.update(schema[k])
    out = _compact(dict(schema, type="object") if schema.get("type") in (None, "object") else schema, defs)
    if out.get("type") != "object":
        return {"type": "object", "properties": {}}
    out.setdefault("properties", {})
    out.pop("description", None)
    return out


def to_openai(server_key: str, tool: dict, name: Optional[str] = None) -> dict:
    """One MCP `tools/list` entry -> one OpenAI function schema (compact)."""
    n = name or tool_name(server_key, str(tool.get("name") or ""))
    desc = tool.get("description") or tool.get("title") or ""
    return {"type": "function", "function": {"name": n, "description": short_description(desc),
                                              "parameters": compact_schema(tool.get("inputSchema"))}}


def is_read_only(tool: dict) -> bool:
    ann = tool.get("annotations")
    return bool(isinstance(ann, dict) and ann.get("readOnlyHint") is True and ann.get("destructiveHint") is not True)


# ------------------------------------------------------------------------------------------------- results
_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}


def format_result(result: Any, save_dir: Optional[Path] = None, max_chars: int = 20000) -> tuple:
    """(text, is_error) for a `tools/call` result. Images are written to save_dir and mentioned by path."""
    if not isinstance(result, dict):
        return (str(result)[:max_chars], False)
    parts: List[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind == "image":
            parts.append(_save_media(item, save_dir, "Image"))
        elif kind == "audio":
            parts.append(f"(audio result, {item.get('mimeType', 'audio')}, not shown)")
        elif kind == "resource":
            res = item.get("resource") or {}
            if isinstance(res.get("text"), str):
                parts.append(res["text"])
            elif res.get("blob"):
                parts.append(_save_media({"data": res["blob"], "mimeType": res.get("mimeType", "")}, save_dir, "File"))
            else:
                parts.append(f"(resource {res.get('uri', '')})")
        elif kind == "resource_link":
            parts.append(f"(link: {item.get('uri', '')} {item.get('name', '')})".strip())
    text = "\n".join(p for p in parts if p).strip()
    if not text and result.get("structuredContent") is not None:
        text = json.dumps(result["structuredContent"], ensure_ascii=False, default=str)
    if len(text) > max_chars:
        head, tail = int(max_chars * 0.75), int(max_chars * 0.2)
        text = (f"{text[:head]}\n[... {len(text) - head - tail} characters left out; ask for a narrower part ...]\n"
                f"{text[-tail:]}")
    return (text or "(no output)"), bool(result.get("isError"))


def _save_media(item: dict, save_dir: Optional[Path], what: str) -> str:
    mime = str(item.get("mimeType") or "")
    if save_dir is None:
        return f"({what} result, {mime or 'binary'}, not saved)"
    try:
        raw = base64.b64decode(str(item.get("data") or ""), validate=False)
        if not raw:
            return f"({what} result was empty)"
        ext = _EXT.get(mime.lower(), ".bin")
        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        p = save_dir / f"mcp_{hashlib.sha1(raw).hexdigest()[:12]}{ext}"
        p.write_bytes(raw)
        hint = " Look at it with analyze_image; send it with send_file." if ext != ".bin" else ""
        return f"{what} saved: {p.as_posix()}.{hint}"
    except Exception as e:                                           # noqa: BLE001
        return f"({what} result could not be saved: {e})"
