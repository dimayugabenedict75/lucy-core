"""Tool routing for small models: show only a few tool schemas per turn, let the model ask for more.

A 4-12B model gets worse as the tool list grows, so each turn starts with a small ALWAYS set plus the tool
families the user's words point at. `tool_search(query)` (handled inside the agent loop) adds more mid-turn, and
the loop also auto-adds a real tool the model calls by name even though it was not in the prompt.

Not a tool module (no SCHEMA/TOOLS here), so registry discovery ignores it. Pure functions: easy to test.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

TOOL_SEARCH_NAME = "tool_search"

TOOL_SEARCH = {
    "type": "function",
    "function": {
        "name": TOOL_SEARCH_NAME,
        "description": ("Find more tools by keyword (e.g. 'google drive', 'run a command', 'reminder', 'click'). "
                        "The tools it finds become available right after, so call them next."),
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string", "description": "What you need to do, in a few words."}},
                       "required": ["query"]},
    },
}

# Always in the prompt: read/write/edit files, memory, and the sticker (the persona uses it all the time).
ALWAYS = ("read_local_file", "write_local_file", "edit_local_file", "remember", "recall_memory", "send_sticker")

_F = re.IGNORECASE
# (family, pattern on the user's words, tools it brings in)
FAMILIES: List[tuple] = [
    ("web", re.compile(r"\b(search|google|look\s?up|latest|news|weather|price|prices|online|internet|website|web\s?page|"
                       r"browse|url|link|article|who is|what is|how much|current|today)\b|https?://", _F),
     ("web_search", "web_extract")),
    ("files", re.compile(r"\b(files?|folders?|director(?:y|ies)|paths?|list|find|where\s+is|locate|open|exists?|"
                         r"desktop|downloads?|documents)\b|[A-Za-z]:[\\/]|/Users/|/home/|"
                         r"\b[\w\-]+\.(?:py|js|ts|md|txt|json|ya?ml|html|css|docx?|xlsx?|pptx?|pdf|csv|zip|png|jpe?g|gif|wav|mp3)\b", _F),
     ("list_local_files", "find_file", "send_file")),
    ("send", re.compile(r"\b(send|attach|share|upload|give me the file|download)\b", _F), ("send_file", "list_local_files", "find_file")),
    ("image", re.compile(r"\b(images?|pictures?|photos?|screenshots?|attached|diagram|logo|what(?:'s| is) in this)\b|"
                         r"\.(?:png|jpe?g|gif|webp|bmp)\b", _F), ("analyze_image",)),
    ("voice", re.compile(r"\b(say|speak|voice|read\s+(?:it\s+)?aloud|read\s+(?:it\s+)?out|tts|talk to me|out loud)\b", _F), ("send_tts",)),
    ("reminders", re.compile(r"\b(remind|reminders?|alarm|timer|schedule|follow[- ]?up|every\s+(?:day|week|morning|hour)|"
                             r"tomorrow|tonight|later|quiet hours|mute|check in|check-in)\b", _F),
     ("set_reminder", "schedule_task", "manage_reminders", "proactive_control")),
    ("memory", re.compile(r"\b(forget|memor(?:y|ies)|what do you know about me)\b", _F), ("forget", "search_conversations")),
    ("history", re.compile(r"\b(earlier|last time|we talked|we discussed|yesterday|previous(?:ly)?|before|remember when|"
                           r"conversations?|chat history|you said|i told you)\b", _F), ("search_conversations",)),
    ("drive", re.compile(r"\b(google|drive|gdrive|g-drive|docs?)\b", _F), ("list_drive_files", "create_google_doc", "get_doc_content")),
    ("shell", re.compile(r"\b(run|execute|command|terminal|powershell|cmd|bash|shell|install|pip|npm|git|build|compile|"
                         r"pytest|test|script|process|kill|restart|service)\b", _F), ("run_shell_command",)),
    ("desktop", re.compile(r"\b(click|double[- ]?click|mouse|cursor|keyboard|type\s+(?:in|into)|press|hotkey|minimi[sz]e|"
                           r"window|desktop|screen)\b", _F), ("cua_cursor", "cua_mouse", "cua_keyboard")),
]


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):                       # multimodal message: keep the text parts, note images
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    parts.append(str(p.get("text", "")))
                elif p.get("type") == "image_url":
                    parts.append("image attached")
        return " ".join(parts)
    return ""


def select_tools(user_text: str, history: Optional[Iterable[dict]] = None,
                 available: Optional[Iterable[str]] = None, history_turns: int = 2) -> List[str]:
    """Names of the tools to show this turn: ALWAYS + the families the user's words (and their last couple of
    messages, so "do it" keeps the context) point at. Order: ALWAYS first, then families in the order above."""
    text = str(user_text or "")
    prior = [_text_of(m.get("content")) for m in (history or []) if isinstance(m, dict) and m.get("role") == "user"]
    context = " ".join([text] + prior[-history_turns:]) if history_turns else text
    names: List[str] = list(ALWAYS)
    for _family, pattern, tools in FAMILIES:
        if pattern.search(context):
            for t in tools:
                if t not in names:
                    names.append(t)
    if available is not None:
        have = set(available)
        names = [n for n in names if n in have]
    return names


class ToolCatalog:
    """Every tool the model could use, searchable by keyword. Lives next to the loop so tool_search can add schemas."""

    def __init__(self, tools: Iterable[dict]):
        self._by_name: Dict[str, dict] = {}
        for t in tools:
            name = (t.get("function") or {}).get("name")
            if name and name != TOOL_SEARCH_NAME:
                self._by_name[name] = t

    def names(self) -> List[str]:
        return list(self._by_name)

    def get(self, name: str) -> Optional[dict]:
        return self._by_name.get(name)

    def search(self, query: str, limit: int = 4) -> List[dict]:
        q = str(query or "").lower()
        words = {w for w in re.findall(r"[a-z0-9]+", q) if len(w) >= 3}
        scores: Dict[str, int] = {}
        for _family, pattern, tools in FAMILIES:
            if pattern.search(q):
                for t in tools:
                    scores[t] = scores.get(t, 0) + 5
        for name, spec in self._by_name.items():
            fn = spec["function"]
            name_words = set(re.findall(r"[a-z0-9]+", name.lower()))
            desc = str(fn.get("description", "")).lower()
            for w in words:
                if w in name_words or w in name.lower():
                    scores[name] = scores.get(name, 0) + 3
                elif w in desc:
                    scores[name] = scores.get(name, 0) + 1
        ranked = sorted((n for n in scores if n in self._by_name), key=lambda n: (-scores[n], n))
        return [self._by_name[n] for n in ranked[:limit]]


def describe_tools(specs: Iterable[dict]) -> str:
    """One short line per tool: 'name - first sentence of its description'."""
    lines = []
    for s in specs:
        fn = s["function"]
        first = re.split(r"(?<=[.!?])\s", str(fn.get("description", "")).strip(), maxsplit=1)[0]
        lines.append(f"- {fn['name']}: {first[:140]}")
    return "\n".join(lines)
