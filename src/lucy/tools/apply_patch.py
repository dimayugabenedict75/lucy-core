"""apply_patch: change ONE file with several edits at once, all-or-nothing.

Two ways to say what to change (the model picks whichever it writes more reliably):
  * edits=[{old, new}, ...]   exact text pairs (applied in order, tolerant like edit_local_file: line endings, trailing
                              whitespace)
  * diff="@@ ... @@ ..."      a unified diff for this file; hunk line numbers may be wrong, hunks are located by their
                              content (exact, then ignoring trailing whitespace, then with the outer context trimmed)

One file per call on purpose: the safety layer sees the target through `file_path`, so path rules, undo snapshots and
approval all work exactly as for edit_local_file. A diff naming several files is refused.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import re
from pathlib import Path
from typing import List, Optional, Tuple

from lucy import paths as _paths
from lucy.server import file_guard

from ._common import outside_root, resolve_path

MAX_BYTES = 5_000_000
DIFF_CHARS = 3500

SCHEMA = {
    "type": "function",
    "function": {
        "name": "apply_patch",
        "description": ("Change ONE file with several edits at once: give 'edits' (old/new text pairs) or a unified 'diff'. "
                        "All edits are applied or none, the result is checked and the diff is shown."),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "The file to change (must already exist)."},
                "edits": {
                    "type": "array",
                    "description": "Changes in order. old = exact existing text (it must match once unless replace_all), new = replacement.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old": {"type": "string", "description": "Exact text to replace."},
                            "new": {"type": "string", "description": "Replacement text."},
                            "replace_all": {"type": "boolean", "description": "Replace every match of old."},
                        },
                        "required": ["old", "new"],
                    },
                },
                "diff": {"type": "string", "description": "Instead of edits: a unified diff (@@ hunks) for this one file. Line numbers may be approximate."},
            },
            "required": ["file_path"],
        },
    },
}
TIER = "write"
LABEL = "edit"


# ------------------------------------------------------------------------------------------------ edits
_ALIASES = {"old": ("old", "old_string", "old_text", "search", "find", "before"),
            "new": ("new", "new_string", "new_text", "replace", "replacement", "after")}


def _pick(item: dict, key: str):
    for k in _ALIASES[key]:
        if k in item and item[k] is not None:
            return item[k]
    return None


def _normalise_edits(args: dict) -> Tuple[Optional[list], str]:
    edits = args.get("edits")
    if isinstance(edits, str):
        try:
            edits = json.loads(edits)
        except ValueError:
            return None, "Error: 'edits' must be a list like [{\"old\": \"...\", \"new\": \"...\"}]."
    if edits is None and (args.get("old_string") is not None or args.get("old") is not None):
        edits = [{"old": args.get("old_string", args.get("old")), "new": args.get("new_string", args.get("new", ""))}]
    if edits is None:
        return None, ""
    if isinstance(edits, dict):
        edits = [edits]
    if not isinstance(edits, list) or not edits:
        return None, "Error: 'edits' must be a non-empty list of {old, new} pairs."
    out = []
    for i, it in enumerate(edits, 1):
        if not isinstance(it, dict):
            return None, f"Error: edit #{i} must be an object with 'old' and 'new'."
        old, new = _pick(it, "old"), _pick(it, "new")
        if old is None or new is None:
            return None, f"Error: edit #{i} needs both 'old' and 'new' (use new=\"\" to delete text)."
        ra = it.get("replace_all", False)
        ra = ra.strip().lower() == "true" if isinstance(ra, str) else bool(ra)
        out.append((str(old), str(new), ra))
    return out, ""


def _apply_edits(text: str, edits: list) -> Tuple[Optional[str], str]:
    for i, (old, new, ra) in enumerate(edits, 1):
        new_text, msg = file_guard.apply_edit(text, old, new, ra)
        if new_text is None:
            return None, (f"Error: edit #{i} of {len(edits)} failed, so NOTHING was changed. "
                          + msg.replace("Error: ", "", 1).replace("'old_string'", "'old'"))
        text = new_text
    return text, ""


# ------------------------------------------------------------------------------------------------ unified diff
_HUNK = re.compile(r"^@@+\s*-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s*@@+")
_BARE_HUNK = re.compile(r"^@@")


def parse_unified(diff: str) -> Tuple[Optional[List[dict]], str]:
    """-> (hunks, "") or (None, error). A hunk = {"start": old start line or None, "lines": [(op, text)]}.
    Everything before the first '@@' (diff --git, index, ---/+++ names) is ignored; a second file header is refused."""
    diff = diff.replace("\r\n", "\n").strip("\n")
    m = re.match(r"^```[a-z]*\n(.*?)\n?```$", diff, re.DOTALL)
    if m:
        diff = m.group(1)
    lines = diff.split("\n")
    n = len(lines)
    pairs = gits = 0
    i, hunks, cur = 0, [], None
    while i < n:
        ln = lines[i]
        if ln.startswith("diff --git "):
            gits += 1
            cur = None
        elif ln.startswith("--- ") and i + 2 < n and lines[i + 1].startswith("+++ ") and _BARE_HUNK.match(lines[i + 2]):
            pairs += 1                                          # a real file header: ---/+++ followed by a hunk
            cur = None
            i += 2
            continue
        elif _BARE_HUNK.match(ln):
            hm = _HUNK.match(ln)
            cur = {"start": int(hm.group(1)) if hm else None, "lines": []}
            hunks.append(cur)
        elif cur is not None:
            if ln.startswith("\\"):
                pass                                            # "\ No newline at end of file"
            elif ln[:1] in (" ", "+", "-"):
                cur["lines"].append((ln[0], ln[1:]))
            elif ln == "":
                cur["lines"].append((" ", ""))                  # a blank context line whose leading space got lost
            else:
                cur["lines"].append((" ", ln))                  # a context line without its leading space
        i += 1
    if max(pairs, gits) > 1:
        return None, ("Error: this diff touches more than one file. apply_patch changes ONE file per call: "
                      "send one call per file.")
    for h in hunks:                                             # a trailing blank "context" line is just the final newline
        while h["lines"] and h["lines"][-1] == (" ", ""):
            h["lines"].pop()
    hunks = [h for h in hunks if any(op in "+-" for op, _t in h["lines"])]
    if not hunks:
        return None, "Error: the diff has no changes. Use '@@ -line,count +line,count @@' hunks with ' ', '-' and '+' lines."
    return hunks, ""


def _eq(a: str, b: str, tier: int) -> bool:
    return a == b if tier == 0 else a.rstrip() == b.rstrip()


def _find_block(lines: List[str], block: List[str], expected: Optional[int]) -> Tuple[Optional[int], str]:
    n = len(block)
    if n == 0 or n > len(lines):
        return None, ""
    for tier in (0, 1):
        found = [i for i in range(len(lines) - n + 1) if all(_eq(lines[i + k], block[k], tier) for k in range(n))]
        if not found:
            continue
        if len(found) == 1:
            return found[0], ""
        if expected is not None:
            return min(found, key=lambda p: abs(p - expected)), ""
        return None, "ambiguous:" + ", ".join(str(p + 1) for p in found[:6])
    return None, ""


_TRIMS = [(0, 0), (1, 0), (0, 1), (1, 1), (2, 0), (0, 2), (2, 2), (3, 3)]


def _apply_hunks(text: str, hunks: List[dict]) -> Tuple[Optional[str], str]:
    lines = text.split("\n")
    offset = 0
    for no, h in enumerate(hunks, 1):
        ops = h["lines"]
        expected = (h["start"] - 1 + offset) if h["start"] else None
        placed, ambiguous = False, ""
        for lead, trail in _TRIMS:
            seg = ops[lead:len(ops) - trail] if trail else ops[lead:]
            if (lead and any(op != " " for op, _t in ops[:lead])) or (trail and any(op != " " for op, _t in ops[-trail:])):
                continue                                         # only context lines may be trimmed
            old = [t for op, t in seg if op in " -"]
            new = [t for op, t in seg if op in " +"]
            if not old:
                if expected is None:
                    continue
                at = max(0, min(expected + lead, len(lines)))   # pure insertion: trust the line number
            else:
                at, why = _find_block(lines, old, None if expected is None else expected + lead)
                if at is None:
                    ambiguous = ambiguous or why
                    continue
            lines[at:at + len(old)] = new
            offset += len(new) - len(old)
            placed = True
            break
        if not placed:
            if ambiguous.startswith("ambiguous:"):
                return None, (f"Error: hunk #{no} of {len(hunks)} matches several places (lines {ambiguous[10:]}), so NOTHING was "
                              "changed. Add more unchanged context lines around the change, or use edits.")
            first = next((t for op, t in ops if op in " -" and t.strip()), "")
            return None, (f"Error: hunk #{no} of {len(hunks)} does not match the file, so NOTHING was changed. "
                          f"Its first line was {first.strip()[:80]!r}. Read the file again (read_local_file) and copy the "
                          "unchanged lines exactly, or use edits.")
    return "\n".join(lines), ""


# ------------------------------------------------------------------------------------------------ run
def _stat(old: str, new: str) -> Tuple[int, int]:
    add = rem = 0
    for ln in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
        if ln.startswith("+") and not ln.startswith("+++"):
            add += 1
        elif ln.startswith("-") and not ln.startswith("---"):
            rem += 1
    return add, rem


def _show_diff(old: str, new: str, name: str) -> str:
    d = list(difflib.unified_diff(old.splitlines(), new.splitlines(), fromfile=name, tofile=name, lineterm="", n=2))
    out = "\n".join(d)
    return out if len(out) <= DIFF_CHARS else out[:DIFF_CHARS].rsplit("\n", 1)[0] + "\n[... diff cut ...]"


def _run(args: dict) -> str:
    raw = str(args.get("file_path") or "")
    target: Optional[Path] = resolve_path(raw)
    if target is None:
        return outside_root(raw)
    if not target.exists() or not target.is_file():
        base = file_guard.not_found_message(raw, _paths.ROOT).rstrip(".")
        return "Error: " + base[0].lower() + base[1:] + ". apply_patch only changes existing files: create new ones with write_local_file."
    try:
        if target.stat().st_size > MAX_BYTES:
            return "Error: that file is too large for apply_patch (over 5 MB)."
        text, crlf = file_guard.read_text_keep_style(target)
    except (OSError, ValueError) as e:
        return f"Error: {e}"

    edits, err = _normalise_edits(args)
    if err:
        return err
    diff = args.get("diff")
    if edits is None and not (isinstance(diff, str) and diff.strip()):
        return "Error: give either 'edits' (a list of {old, new}) or a unified 'diff'."
    if edits is not None and isinstance(diff, str) and diff.strip():
        return "Error: give 'edits' OR 'diff', not both."
    if edits is not None:
        new_text, err = _apply_edits(text, edits)
        count = len(edits)
    else:
        hunks, err = parse_unified(str(diff))
        if hunks is None:
            return err
        new_text, err = _apply_hunks(text, hunks)
        count = len(hunks)
    if new_text is None:
        return err
    if new_text == text:
        return f"No change: the edits leave {target.as_posix()} exactly as it was."
    problem = file_guard.validate_content(target, new_text)
    if problem:
        return problem
    bak = file_guard.backup_file(target, _paths.ROOT)
    try:
        file_guard.write_text_atomic(target, new_text, crlf)
    except OSError as e:
        return f"Error: could not write {target.as_posix()}: {e}"
    add, rem = _stat(text, new_text)
    extra = ""
    try:
        rel = target.resolve().relative_to(_paths.ROOT.resolve()).as_posix()
        if rel.startswith("src/") and target.suffix == ".py":
            extra = " This is Lucy Core code: the change takes effect after Lucy Core restarts."
        elif rel.startswith(".core/personas/"):
            extra = " Persona changes take effect after Lucy Core restarts."
    except ValueError:
        pass
    unit = "edit" if edits is not None else "hunk"
    return (f"Patched {target.as_posix()}: {count} {unit}{'s' if count != 1 else ''} applied (+{add} -{rem} lines)."
            f"{' Backup saved (' + bak.name + ').' if bak else ''}{extra}\n{_show_diff(text, new_text, target.name)}")


async def handler(args, ctx):
    return await asyncio.to_thread(_run, args or {})
