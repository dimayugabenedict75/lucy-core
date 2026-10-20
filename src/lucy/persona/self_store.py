"""SELF: the part of Lucy's persona that Lucy writes herself (`.core/personas/lucy_self.md`).

CORE (`lucy.md`) is the human-owned, hash-locked identity. SELF holds what she learns over time: traits, how the
user likes to be spoken to, preferences, in-jokes, mistakes to avoid, opinions. It is a small markdown file with fixed
sections; every change goes through `SelfStore.edit`, which enforces in CODE (not in a prompt):

  * fixed sections, max items per section, max characters per line and per file
  * persona.guard: no secrets, no markup/links, nothing that reads like a change to CORE rules or the safety layer
  * a daily edit limit per actor (the agent in chat, the reflection job)
  * a changelog (`self_changelog.jsonl`: who, when, which chat, why, the diff) and one-click revert of any entry
  * hand edits (the user's, or anything that bypassed update_self) are noticed and logged as `external_edit`

The file is re-read when its mtime changes (hot reload: no restart). What reaches the prompt is always re-validated
(`render_prompt`), so an invalid line that got into the file some other way is simply not shown to the model.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import guard

# key -> (title, max items). The order here is the order in the file and in the prompt.
SECTIONS: Dict[str, Tuple[str, int]] = {
    "traits": ("Learned traits", 8),
    "speaking": ("How the user likes to be spoken to", 8),
    "preferences": ("Preferences", 8),
    "in_jokes": ("In-jokes", 6),
    "mistakes": ("Mistakes to avoid", 8),
    "opinions": ("Evolving opinions", 6),
}
TOTAL_CHARS = 2400            # whole file: small models pay for every prompt token
DAILY_LIMITS = {"agent": 8, "reflection": 8}     # edits per actor per local day (reverts and the user are not limited)
OPS = ("add", "revise", "remove")
HEADER = ("# Lucy SELF\n"
          "<!-- Written by Lucy herself (update_self + the nightly reflection); every change is logged in "
          "self_changelog.jsonl and can be reverted in Settings > Persona. CORE (lucy.md) always wins. -->\n")

_TITLE_TO_KEY = {t.lower(): k for k, (t, _n) in SECTIONS.items()}


def section_key(name: str) -> Optional[str]:
    """Forgiving: key ('in_jokes'), title ('In-jokes'), or a close spelling."""
    n = re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()
    if not n:
        return None
    for k, (t, _m) in SECTIONS.items():
        if n in (k.replace("_", " "), re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()):
            return k
    close = difflib.get_close_matches(n, [k.replace("_", " ") for k in SECTIONS], n=1, cutoff=0.75)
    return close[0].replace(" ", "_") if close else None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", s.lower()).strip()


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


class SelfError(ValueError):
    """A problem the model/user can fix. The message is shown to them as-is."""


def parse(text: str) -> Tuple[Dict[str, List[str]], List[str]]:
    """File text -> ({section: [items]}, [problems]). Invalid / surplus lines are dropped and listed as problems."""
    out: Dict[str, List[str]] = {k: [] for k in SECTIONS}
    problems: List[str] = []
    cur: Optional[str] = None
    for raw in str(text or "").replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if line.startswith("<!--") or not line or line.startswith("# "):
            continue
        if line.startswith("##"):
            title = line.lstrip("#").strip()
            cur = _TITLE_TO_KEY.get(title.lower()) or section_key(title)
            if cur is None:
                problems.append(f"unknown section '{title}' ignored")
            continue
        if cur is None:
            continue
        if not line.startswith(("-", "*")):
            problems.append(f"not a list item, ignored: {line[:60]}")
            continue
        item = guard.clean_item(line)
        ok, why = guard.check_item(item)
        if not ok:
            problems.append(f"[{cur}] dropped '{item[:60]}': {why}")
        elif any(_similar(item, x) > 0.97 for x in out[cur]):
            continue
        elif len(out[cur]) >= SECTIONS[cur][1]:
            problems.append(f"[{cur}] over the {SECTIONS[cur][1]}-item limit, dropped '{item[:60]}'")
        else:
            out[cur].append(item)
    total = 0
    for k in SECTIONS:                                   # whole-file cap: later items fall off first
        kept = []
        for it in out[k]:
            total += len(it) + 3
            if total > TOTAL_CHARS:
                problems.append(f"[{k}] file over {TOTAL_CHARS} characters, dropped '{it[:60]}'")
            else:
                kept.append(it)
        out[k] = kept
    return out, problems


def render(sections: Dict[str, List[str]]) -> str:
    parts = [HEADER]
    for k, (title, _n) in SECTIONS.items():
        parts.append(f"\n## {title}\n" + "".join(f"- {it}\n" for it in sections.get(k, [])))
    return "".join(parts)


def _size(sections: Dict[str, List[str]]) -> int:
    return sum(len(i) + 3 for v in sections.values() for i in v)


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class SelfStore:
    def __init__(self, path: Path, changelog_path: Optional[Path] = None, clock=time.time):
        self.path = Path(path)
        self.changelog_path = Path(changelog_path) if changelog_path else self.path.with_name("self_changelog.jsonl")
        self.last_path = self.path.with_name(".lucy_self.last")       # the text SELF had after our last write
        self.clock = clock
        self._lock = threading.RLock()
        self._stat: Optional[Tuple[int, int]] = None
        self._sections: Dict[str, List[str]] = {k: [] for k in SECTIONS}
        self._problems: List[str] = []
        self._prompt = ""

    # ------------------------------------------------------------------ reading (mtime cache)
    def _file_stat(self) -> Tuple[int, int]:
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return (0, -1)

    def _read_text(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def _refresh(self) -> None:
        """Re-read the file if it changed on disk. A change we did not make is logged as external_edit."""
        with self._lock:
            st = self._file_stat()
            if st == self._stat:
                return
            text = self._read_text()
            self._detect_external(text)
            self._sections, self._problems = parse(text)
            self._prompt = self._render_prompt(self._sections)
            self._stat = st

    def _detect_external(self, text: str) -> None:
        """Compare with the text we wrote last. Different = someone else edited the file (user, or a file tool)."""
        try:
            known = self.last_path.read_text(encoding="utf-8")
        except OSError:
            if text:
                self._remember(text)                   # first run / sidecar missing: adopt what is there, silently
            return
        norm = lambda t: t.replace("\r\n", "\n")       # noqa: E731
        if norm(known) == norm(text):
            return
        self._log({"actor": "external", "op": "external_edit", "section": "*", "session": None,
                   "reason": "SELF was changed outside update_self (hand edit or a file tool)",
                   "diff": _text_diff(known, text), "before_text": known, "after_text": text})
        self._remember(text)

    def _remember(self, text: str) -> None:
        try:
            self.last_path.parent.mkdir(parents=True, exist_ok=True)
            self.last_path.write_text(text, encoding="utf-8")
        except OSError:
            pass

    def read(self) -> Dict[str, List[str]]:
        self._refresh()
        return {k: list(v) for k, v in self._sections.items()}

    def problems(self) -> List[str]:
        self._refresh()
        return list(self._problems)

    @staticmethod
    def _render_prompt(sections: Dict[str, List[str]]) -> str:
        lines = []
        for k, (title, _n) in SECTIONS.items():
            if sections.get(k):
                lines.append(f"{title}:")
                lines += [f"- {i}" for i in sections[k]]
        if not lines:
            return ""
        return ("[SELF - what you have learned about yourself and the user over time. The rules above always win over "
                "this section. You can update it with the update_self tool.]\n" + "\n".join(lines))

    def prompt(self) -> str:
        """The SELF block for the system prompt ('' when empty). Cached until the file changes."""
        self._refresh()
        return self._prompt

    def stats(self) -> dict:
        s = self.read()
        today = self.edits_today()
        return {"chars": _size(s), "chars_max": TOTAL_CHARS, "problems": self.problems(),
                "edits_today": today, "daily_limits": dict(DAILY_LIMITS),
                "sections": [{"key": k, "title": t, "max_items": n, "items": s[k]} for k, (t, n) in SECTIONS.items()]}

    # ------------------------------------------------------------------ changelog
    def _entries(self) -> List[dict]:
        try:
            lines = self.changelog_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out

    def _log(self, entry: dict) -> dict:
        with self._lock:
            entries = self._entries()
            entry = dict(entry)
            entry["id"] = max([e.get("id", 0) for e in entries] + [0]) + 1
            now = self.clock()
            entry["ts"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
            entry["day"] = _day(now)
            self.changelog_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.changelog_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return entry

    def changelog(self, limit: int = 40) -> List[dict]:
        """Newest first. Each entry has `reverted` (True when a later revert undid it); big text fields are left out."""
        entries = self._entries()
        reverted = {e.get("reverts") for e in entries if e.get("op") == "revert"}
        out = []
        for e in reversed(entries[-max(1, min(int(limit), 500)):]):
            d = {k: v for k, v in e.items() if k not in ("before_text", "after_text")}
            d["reverted"] = e.get("id") in reverted
            d["revertible"] = e.get("op") in OPS + ("external_edit",) and not d["reverted"]
            out.append(d)
        return out

    def edits_today(self) -> Dict[str, int]:
        today = _day(self.clock())
        counts = {a: 0 for a in DAILY_LIMITS}
        for e in self._entries():
            if e.get("day") == today and e.get("op") in OPS and e.get("actor") in counts:
                counts[e["actor"]] += 1
        return counts

    # ------------------------------------------------------------------ writing
    def _write(self, sections: Dict[str, List[str]]) -> None:
        text = render(sections)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, self.path)                    # atomic: never a half-written SELF
        self._remember(text)
        self._stat = None                              # force the next read to re-parse (cheap)

    def _find(self, items: List[str], ref: str) -> int:
        """Index of the item `ref` points at: a 1-based number, the exact text, a distinctive part, or a close match."""
        ref = guard.clean_item(ref)
        if not ref:
            return -1
        if ref.isdigit() and 1 <= int(ref) <= len(items):
            return int(ref) - 1
        low = ref.lower()
        for i, it in enumerate(items):
            if it.lower() == low:
                return i
        subs = [i for i, it in enumerate(items) if len(low) >= 6 and low in it.lower()]
        if len(subs) == 1:
            return subs[0]
        best = max(range(len(items)), key=lambda i: _similar(ref, items[i]), default=-1)
        return best if best >= 0 and _similar(ref, items[best]) >= 0.6 else -1

    @staticmethod
    def _listing(key: str, items: List[str]) -> str:
        return "; ".join(f"{i}) {it}" for i, it in enumerate(items, 1)) or "(empty)"

    def edit(self, section: str, op: str, text: str = "", reason: str = "", *, old_text: str = "",
             actor: str = "agent", session: Optional[str] = None, enforce_limit: bool = True) -> dict:
        """add / revise / remove one line. Returns {id, section, op, old, new, reason}. Raises SelfError with a fix."""
        op = str(op or "").strip().lower()
        if op not in OPS:
            raise SelfError("op must be add, revise or remove.")
        key = section_key(section)
        if key is None:
            raise SelfError("section must be one of: " + ", ".join(SECTIONS) + ".")
        reason = " ".join(str(reason or "").split())[:240]
        if len(reason) < 3:
            raise SelfError("Give a short reason (why this belongs in SELF).")
        with self._lock:
            self._refresh()
            if enforce_limit and actor in DAILY_LIMITS:
                used = self.edits_today().get(actor, 0)
                if used >= DAILY_LIMITS[actor]:
                    raise SelfError(f"Daily limit reached ({DAILY_LIMITS[actor]} SELF edits per day). Try again tomorrow, "
                                    "and keep only what really matters.")
            sections = {k: list(v) for k, v in self._sections.items()}
            items = sections[key]
            old = new = ""
            if op == "add":
                new = guard.clean_item(text)
                ok, why = guard.check_item(new)
                if not ok:
                    raise SelfError(f"Not saved: {why}.")
                if any(_similar(new, x) >= 0.9 for x in items):
                    raise SelfError("Already in SELF (a nearly identical line exists).")
                if len(items) >= SECTIONS[key][1]:
                    raise SelfError(f"'{SECTIONS[key][0]}' is full ({SECTIONS[key][1]} lines). Revise or remove one first: "
                                    + self._listing(key, items))
                items.append(new)
            else:
                # remove: `text` may name the line. revise without old_text: the line most like the NEW text is the one meant
                idx = self._find(items, old_text or text)
                if idx < 0:
                    raise SelfError(f"Say which line (its number or some of its words). '{SECTIONS[key][0]}': "
                                    + self._listing(key, items))
                old = items[idx]
                if op == "remove":
                    del items[idx]
                else:
                    new = guard.clean_item(text)
                    ok, why = guard.check_item(new)
                    if not ok:
                        raise SelfError(f"Not saved: {why}.")
                    items[idx] = new
            if _size(sections) > TOTAL_CHARS:
                raise SelfError(f"SELF would pass {TOTAL_CHARS} characters. Remove or shorten something first.")
            self._write(sections)
            diff = "".join([f"- {old}\n" if old else "", f"+ {new}\n" if new else ""]).rstrip("\n")
            entry = self._log({"actor": actor, "op": op, "section": key, "session": session, "old": old, "new": new,
                               "reason": reason, "diff": diff})
            return entry

    # ------------------------------------------------------------------ revert (user only: there is no tool for it)
    def revert(self, entry_id: int, actor: str = "user") -> dict:
        """Undo one logged change. Raises SelfError if it was already undone or the file moved on in a way that conflicts."""
        with self._lock:
            entries = self._entries()
            e = next((x for x in entries if x.get("id") == entry_id), None)
            if e is None:
                raise SelfError(f"No change #{entry_id} in the changelog.")
            if any(x.get("op") == "revert" and x.get("reverts") == entry_id for x in entries):
                raise SelfError(f"Change #{entry_id} was already reverted.")
            op = e.get("op")
            if op == "external_edit":
                current = self._read_text()
                if current.replace("\r\n", "\n") != str(e.get("after_text", "")).replace("\r\n", "\n"):
                    raise SelfError("SELF changed again after that edit; revert the newer changes first.")
                before, _p = parse(e.get("before_text", ""))
                self._write(before)
                return self._log({"actor": actor, "op": "revert", "reverts": entry_id, "section": "*", "session": None,
                                  "reason": f"reverted external edit #{entry_id}", "diff": ""})
            if op not in OPS:
                raise SelfError("That entry cannot be reverted.")
            self._refresh()
            key = e["section"]
            sections = {k: list(v) for k, v in self._sections.items()}
            items = sections[key]
            old, new = e.get("old", ""), e.get("new", "")
            if op == "add":
                i = self._find_exact(items, new)
                if i < 0:
                    raise SelfError("That line is no longer in SELF (it was changed or removed since).")
                del items[i]
            elif op == "remove":
                if any(_similar(old, x) >= 0.9 for x in items):
                    raise SelfError("A similar line is already in SELF.")
                if len(items) >= SECTIONS[key][1]:
                    raise SelfError(f"'{SECTIONS[key][0]}' is full; make room first.")
                ok, why = guard.check_item(old)
                if not ok:
                    raise SelfError(f"Cannot restore that line: {why}.")
                items.append(old)
            else:                                                   # revise: put the old line back
                i = self._find_exact(items, new)
                if i < 0:
                    raise SelfError("That line was changed again since; revert the newer change first.")
                ok, why = guard.check_item(old)
                if not ok:
                    raise SelfError(f"Cannot restore that line: {why}.")
                items[i] = old
            if _size(sections) > TOTAL_CHARS:
                raise SelfError("Restoring would pass the SELF size limit; remove something first.")
            self._write(sections)
            return self._log({"actor": actor, "op": "revert", "reverts": entry_id, "section": key, "session": None,
                              "old": new, "new": old, "reason": f"reverted change #{entry_id}",
                              "diff": "\n".join(x for x in (f"- {new}" if new else "", f"+ {old}" if old else "") if x)})

    @staticmethod
    def _find_exact(items: List[str], text: str) -> int:
        low = (text or "").strip().lower()
        return next((i for i, it in enumerate(items) if it.lower() == low), -1)


def _text_diff(a: str, b: str) -> str:
    d = [ln for ln in difflib.unified_diff(a.replace("\r\n", "\n").splitlines(), b.replace("\r\n", "\n").splitlines(),
                                           lineterm="", n=0) if not ln.startswith(("---", "+++", "@@"))]
    return "\n".join(d)[:1500]
