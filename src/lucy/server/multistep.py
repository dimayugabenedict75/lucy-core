"""Multi-step support for the agent loop: promises, plans and the "do what you said" ladder.

Why this exists
---------------
Small local models often end a reply with "I'll run the search now. 🔍" and then simply stop: the
text counts as a finished answer for the model, so no tool is ever called. This module gives the
loop three things to deal with that:

  1. `find_promise(text)`      - spots an announced-but-not-done action. Emoji and other decoration are
                                 ignored (the old check looked only at the last "sentence", which was
                                 the lone emoji, so it never fired). Looks at the last two sentences.
  2. `Promise.queries`         - the search queries the model announced, so the harness can run the
                                 search itself when the model keeps refusing to call the tool.
  3. `update_plan` / `Plan`    - a tiny checklist tool for tasks with several steps. While steps are
                                 pending the loop will not let the turn end on a plain reply.

Everything here is pure (no HTTP, no model) so it can be unit-tested directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

PLAN_TOOL_NAME = "update_plan"

PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": PLAN_TOOL_NAME,
        "description": (
            "Keep a checklist for a task that needs 2 or more steps (research, edit-then-verify, "
            "compare several things). Call it FIRST to list the steps, then again whenever a step is done "
            "or skipped. Send the full list every time. Do not use it for simple one-step requests."),
        "parameters": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "description": "The whole checklist, in order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "What this step does (short)."},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "done", "skipped"]},
                        },
                        "required": ["text"],
                    },
                },
            },
            "required": ["steps"],
        },
    },
}

# ------------------------------------------------------------------------------------ text helpers
# Emoji, pictographs, variation selectors, ZWJ, regional indicators, dingbats.
_DECOR = re.compile(
    "[\U0001F000-\U0001FAFF\U0001F1E6-\U0001F1FF\u2190-\u21FF\u2300-\u23FF\u2600-\u27BF\u2B00-\u2BFF"
    "\uFE0E\uFE0F\u200D\u20E3]+")
_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")


def strip_decor(text: str) -> str:
    return _DECOR.sub("", text)


def sentences(text: str) -> list[str]:
    """Sentences with emoji removed; fragments with no letters or digits are dropped."""
    out = []
    for part in _SPLIT.split(strip_decor(text)):
        part = part.strip(" \t*_>-•")
        if part and re.search(r"\w", part):
            out.append(part)
    return out


# ------------------------------------------------------------------------------------ promise detection
_ACTION = (r"search|look(?:\s+\w+){0,2}?\s+up|look\s+for|take\s+a\s+look|have\s+a\s+look|google|check|read|open|"
           r"edit|update|patch|modify|write|create|save|store|remember|note|record|run|execute|fetch|find|list|"
           r"browse|pull\s+up|dig|familiari[sz]e|review|study|explore|scan|go\s+through|look\s+into|"
           r"investigate|examine|inspect|analy[sz]e|send|compare|gather|collect|grab|query|try|remind|schedule|follow\s*up|"
           r"pull|load|retrieve|reading|checking|running|executing|opening|writing|editing|pulling|loading|searching")
_LEAD = (r"let me|let['’]s|i['’]ll|i will|i['’]m going to|i am going to|allow me to|going to|i['’]m about to|"
         r"i['’]m gonna|i shall|time to|now i|next,? i|first,? i|then i|i['’]ll now|i['’]m now|"
         r"i am currently|i['’]m currently|i['’]m in the process of")
_PROMISE = re.compile(rf"\b(?:{_LEAD})\b[^.!?\n]{{0,80}}?\b(?:{_ACTION})\b", re.IGNORECASE)
_OFFER = re.compile(r"\b(?:if you(?:['’]d)? (?:like|want)|would you like|want me to|shall i|should i|do you want|"
                    r"let me know|feel free)\b", re.IGNORECASE)
_SEARCHY = re.compile(r"\b(?:search|look(?:\s+\w+){0,2}?\s+up|look\s+for|google|browse|find|query)\b", re.IGNORECASE)
_VERB = re.compile(_ACTION, re.IGNORECASE)


@dataclass
class Promise:
    sentence: str                   # the sentence that announced the action
    kind: str                       # "search" | "other"
    verb: str                       # the matched action verb ("search", "read", ...)
    queries: list[str] = field(default_factory=list)    # concrete search queries named in the text


_QUOTED = re.compile(r"[\"“”«]([^\"“”»\n]{3,120})[\"“”»]")
_AFTER_SEARCH = re.compile(
    r"\b(?:search(?:ing)?(?:\s+the\s+web)?(?:\s+for)?|look(?:ing)?\s+(?:up|for)|google|find(?:ing)?)\s+(.+?)"
    r"(?:\s+(?:to (?:see|find|compare|check|get|learn|figure|understand|gather|look|pick|decide)|so that|so i|in order|and then|because|which|that will)\b|[.!?\n]|$)", re.IGNORECASE)
_GENERIC = re.compile(r"^(?:that|it|this|them|those|these|more|some|something|anything|info|information|"
                      r"for that|for it|that up|it up|a bit|now)\W*$", re.IGNORECASE)


def extract_queries(text: str, limit: int = 3) -> list[str]:
    """Search queries named in `text`: quoted strings first, else the phrase after 'search for'."""
    found: list[str] = []
    for q in _QUOTED.findall(text):
        q = q.strip()
        if q and q.lower() not in (x.lower() for x in found):
            found.append(q)
    if not found:
        m = _AFTER_SEARCH.search(strip_decor(text))
        if m:
            q = m.group(1).strip(" \"'“”.,:;")
            if 3 <= len(q) <= 120 and not _GENERIC.match(q):
                found.append(q)
    return found[:limit]


def find_promise(text: str, max_chars: int = 1600) -> Promise | None:
    """An announced-but-not-taken action at the END of a reply, else None.

    Only the last two sentences count, so a finished answer that merely mentions "I'll check" earlier
    isn't flagged; questions and offers ("want me to search?") are not promises."""
    text = (text or "").strip()
    if not text or len(text) > max_chars:
        return None
    sents = sentences(text)
    for s in reversed(sents[-2:]):
        if s.endswith("?") or _OFFER.search(s):
            return None                       # ends by asking / offering: the user is meant to answer
        m = _PROMISE.search(s)
        if m:
            verb = _VERB.search(s, m.start()).group(0).lower()
            kind = "search" if _SEARCHY.search(s) else "other"
            # a promise is often spread over two sentences ("I'll search for X, Y. I'll run the search now.")
            queries = extract_queries(" ".join(sents[-2:])) if kind == "search" else []
            return Promise(s, kind, verb, queries)
    return None


# ------------------------------------------------------------------------------------ plan tracking
_STATUSES = {"pending", "in_progress", "done", "skipped"}
_STATUS_ALIASES = {"complete": "done", "completed": "done", "finished": "done", "todo": "pending",
                   "to_do": "pending", "doing": "in_progress", "active": "in_progress", "current": "in_progress",
                   "skip": "skipped", "cancelled": "skipped", "canceled": "skipped", "blocked": "skipped"}
_MARK = {"pending": "[ ]", "in_progress": "[>]", "done": "[x]", "skipped": "[-]"}


@dataclass
class Plan:
    steps: list[dict] = field(default_factory=list)     # [{"text", "status"}]
    updates: int = 0
    nudges: int = 0

    def set(self, raw_steps) -> str | None:
        """Replace the checklist. Returns an error string, or None when accepted."""
        if not isinstance(raw_steps, list) or not raw_steps:
            return "steps must be a non-empty list of {text, status} objects"
        steps = []
        for item in raw_steps[:12]:
            if isinstance(item, str):
                item = {"text": item}
            if not isinstance(item, dict):
                return "each step must be an object like {\"text\": \"...\", \"status\": \"pending\"}"
            text = str(item.get("text") or item.get("step") or item.get("title") or "").strip()
            if not text:
                return "every step needs a non-empty \"text\""
            status = str(item.get("status") or "pending").strip().lower()
            status = _STATUS_ALIASES.get(status, status)
            steps.append({"text": text[:160], "status": status if status in _STATUSES else "pending"})
        # only one step may be in progress: the first one wins, the rest go back to pending
        seen = False
        for s in steps:
            if s["status"] == "in_progress":
                if seen:
                    s["status"] = "pending"
                seen = True
        self.steps = steps
        self.updates += 1
        return None

    def pending(self) -> list[dict]:
        return [s for s in self.steps if s["status"] in ("pending", "in_progress")]

    def next_step(self) -> str | None:
        p = self.pending()
        return p[0]["text"] if p else None

    def render(self) -> str:
        return "\n".join(f"{i}. {_MARK[s['status']]} {s['text']}" for i, s in enumerate(self.steps, 1))
