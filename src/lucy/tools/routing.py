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

def tool_search_schema(extra: str = "") -> dict:
    """TOOL_SEARCH, with the names of connected MCP servers added to its description (stable text = stable prompt cache)."""
    if not extra:
        return TOOL_SEARCH
    import copy
    t = copy.deepcopy(TOOL_SEARCH)
    t["function"]["description"] += f" Also connected: {extra}."
    return t


# Always in the prompt: read/write/edit files, memory, and the sticker (the persona uses it all the time).
ALWAYS = ("read_local_file", "write_local_file", "edit_local_file", "remember", "recall_memory", "send_sticker")

_F = re.IGNORECASE
# Office wording. A family fires on the noun alone (read) or on noun + intent verb (create / edit): the model then sees 2-4
# Office tools instead of all nine.
_WORD = (r"\b(?:word|docx|document|report|memo|letter|resum[e\u00e9]|cv|essay|proposal|minutes|newsletter|handout|brochure|flyer|"
         r"contract|agreement|invoice|cover letter)\b")
_EXCEL = r"\b(?:excel|xlsx|xlsm|spreadsheet|workbook|worksheet|sheet|budget|tracker|ledger|formulas?|pivot|csv)\b"
_PPT = r"\b(?:powerpoint|pptx|slides?|slide deck|deck|presentation|pitch|keynote)\b"
_PDF = r"\bpdf\b"
_MAKE = (r"\b(?:make|create|write|draft|generate|build|prepare|compose|produce|put together|design|export|convert|turn|new|"
         r"save (?:it )?as)\b")
_CHANGE = (r"\b(?:edit|change|update|fix|replace|add|insert|delete|remove|modify|rename|correct|rewrite|revise|append|reorder|move|"
           r"swap|translate|shorten|expand)\b")


def _intent(noun: str, verb: str):
    """Pattern that needs BOTH an Office noun and an intent verb somewhere in the text (any order)."""
    return re.compile(r"^(?=.*" + noun + r")(?=.*" + verb + r")", re.IGNORECASE | re.DOTALL)


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
    # P8: long-running / repeating background work (the `task` tool). Bare "task" is NOT a trigger (too common).
    ("tasks", re.compile(r"\b(keep (?:an eye|track)|monitor|watch (?:for|over)|background tasks?|in the background|overnight|"
                         r"(?:my|the|your|open|active|running|all) tasks|task (?:list|status|queue)|recurring|cron|deadline|finish (?:this|it|the \w+) by|"
                         r"while i(?:'m| am) (?:away|out|asleep|sleeping|gone)|(?:cancel|stop|resume|continue) (?:the )?task)\b", _F),
     ("task",)),
    ("memory", re.compile(r"\b(forget|memor(?:y|ies)|what do you know about me)\b", _F), ("forget", "search_conversations")),
    ("history", re.compile(r"\b(earlier|last time|we talked|we discussed|yesterday|previous(?:ly)?|before|remember when|"
                           r"conversations?|chat history|you said|i told you)\b", _F), ("search_conversations",)),
    ("drive", re.compile(r"\b(google|drive|gdrive|g-drive|docs?)\b", _F), ("list_drive_files", "create_google_doc", "get_doc_content")),
    ("shell", re.compile(r"\b(run|execute|command|terminal|powershell|cmd|bash|shell|install|pip|npm|git|build|compile|"
                         r"pytest|test|script|process|kill|restart|service)\b", _F), ("run_shell_command",)),
    ("code_explore", re.compile(r"\b(code|codebase|source|repo|repository|project|function|class|method|symbol|grep|refactor|where is .{1,40} (?:defined|used)|"
                                r"find (?:the )?(?:usages?|references?|definition)|who calls|implement(?:ation)?|bug|traceback|stack ?trace|exception)\b", _F),
     ("code_search", "project_map")),
    ("code_patch", re.compile(r"\b(patch|diff|refactor|rename|fix\b.{0,25}\b(?:bug|error|test|code|failure|crash)|implement|add (?:a )?(?:function|method|class|feature)|"
                              r"change the code|several (?:edits|changes))\b", _F), ("apply_patch", "code_search")),
    ("code_files", re.compile(r"\b[\w\-]+\.(?:py|js|jsx|ts|tsx|cs|java|go|rs|cpp|c|h|kt|rb|php|sh|ps1)\b", _F), ("apply_patch", "code_search")),
    ("code_tests", re.compile(r"\b(tests?|pytest|unittest|jest|vitest|dotnet test|npm test|failing|ci|build breaks?|passes|passing)\b", _F), ("run_tests",)),
    ("code_git", re.compile(r"\b(git|commit|branch|stash|checkout|staged|unstaged|uncommitted|what changed|changes since|diff)\b", _F), ("git",)),
    ("code_python", re.compile(r"\b(python repl|in python|run python|calculate|compute|evaluate|crunch|quick script|pandas|numpy|statistics|average of)\b", _F), ("python",)),
    ("code_lint", re.compile(r"\b(lint|linter|format(?:ting)? (?:the )?(?:code|file)|ruff|black|eslint|prettier|code style|pep ?8)\b", _F), ("format_and_lint",)),
    # --- Office: the noun alone brings office_read; noun + a create verb brings the create tool; noun + an edit verb the edit tool ---
    ("word_read", re.compile(_WORD, _F), ("office_read",)),
    ("word_new", _intent(_WORD, _MAKE), ("docx_create", "office_render")),
    ("word_edit", _intent(_WORD, _CHANGE), ("docx_edit", "office_render")),
    ("excel_read", re.compile(_EXCEL, _F), ("office_read",)),
    ("excel_new", _intent(_EXCEL, _MAKE), ("xlsx_create",)),
    ("excel_edit", _intent(_EXCEL, _CHANGE), ("xlsx_edit",)),
    ("ppt_read", re.compile(_PPT, _F), ("office_read",)),
    ("ppt_new", _intent(_PPT, _MAKE), ("pptx_create", "office_render")),
    ("ppt_edit", _intent(_PPT, _CHANGE), ("pptx_edit", "office_render")),
    ("pdf_read", re.compile(_PDF, _F), ("office_read",)),
    ("pdf_new", _intent(_PDF, _MAKE), ("pdf_create", "office_render")),
    # --- P6 SELF persona: corrections about HOW Lucy talks/behaves -> update_self (the prompt also tells her to call it by name) ---
    ("self", re.compile(r"\b(from now on|stop (?:being|doing|saying|using|talking)|don'?t (?:be|talk|sound|say|use|call me)|"
                        r"i (?:like|prefer|want|love|hate) (?:it )?when you|you (?:should|shouldn'?t|always|never|keep|tend to) |"
                        r"your (?:personality|tone|style|habit|habits)|talk to me|speak to me|call me|that was (?:funny|rude|too)|"
                        r"too (?:long|formal|wordy|chatty|blunt)|be more|be less)\b", _F), ("update_self",)),
    ("undo", re.compile(r"\b(undo|revert|restore|roll\s?back|put (?:it|that) back|backups?|previous version)\b", _F),
     ("restore_from_backup",)),
    # --- P7 self-evolution: Lucy's own tools (Lane A) and gated changes to Lucy Core (Lane B) ---
    ("selfdev_tool", re.compile(r"\b(?:(?:make|write|create|build|add|code)\s+(?:yourself\s+)?(?:a\s+|an\s+|your\s+own\s+|new\s+)?tool|"
                                r"new tool|your (?:own )?tools?|user tools?|custom tool|improve (?:the|your|that) tool|"
                                r"(?:disable|switch off|turn off) (?:the|your|that) tool)\b", _F), ("user_tool",)),
    ("selfdev_core", re.compile(r"\b(?:improve|change|fix|upgrade|update|modify|edit|patch)\s+(?:your\s*self|your (?:own )?"
                                r"(?:code|source|harness|core)|lucy core)\b|\bself[- ]?(?:improve|improvement|evolv\w*|"
                                r"modif\w*)|\bcore[_ ]change\b|\b(?:trace (?:review|mining)|improvement ideas)\b", _F),
     ("core_change",)),
    # --- P5 PC agent: words -> the small set of PC tools (the rest stay one tool_search away) ---
    ("pc_apps", re.compile(r"\b(install|uninstall|upgrade|update)\b.{0,30}\b(app|apps|program|software|package)\b|\bwinget\b|"
                          r"\b(install|uninstall|reinstall)\s+[\w.\-]+", _F), ("winget",)),
    ("pc_service", re.compile(r"\b(service|services|daemon|background service|spooler|print spooler|windows service)\b", _F), ("service",)),
    ("pc_process", re.compile(r"\b(process(?:es)?|task manager|not responding|frozen|hung|hang(?:ing)?|using (?:all )?(?:my )?(?:ram|memory|cpu)|"
                             r"kill|force[- ]?close|end task|close\s+[\w.]+\.exe)\b", _F), ("process",)),
    ("pc_task", re.compile(r"\b(scheduled task|task scheduler|schtasks|startup task|runs at (?:login|startup|boot)|cron)\b", _F), ("scheduled_task",)),
    ("pc_registry", re.compile(r"\b(registry|regedit|reg key|hkcu|hklm|hkey)\b", _F), ("registry",)),
    ("pc_info", re.compile(r"\b(disk space|free space|how much space|storage left|drive (?:full|space)|biggest (?:files|folders)|"
                          r"what(?:'s| is) taking up|firewall|event log|event viewer|windows update|updates? (?:pending|available)|"
                          r"crash(?:ed|ing)?|blue ?screen|bsod)\b", _F), ("system_info",)),
    ("pc_files", re.compile(r"\b(back\s?up|backup|copy\s+(?:it|this|the|all|my)|move\s+(?:it|this|the|all|my|to)|duplicate|"
                           r"recycle bin|to the trash|make (?:a )?folder|create (?:a )?folder|new folder)\b", _F), ("file_ops",)),
    ("pc_window", re.compile(r"\b(window|windows (?:open|list)|bring.{0,15}(?:front|focus)|switch to|minimi[sz]e|maximi[sz]e|"
                            r"focus (?:the|on)|which apps? (?:are )?open|windows? (?:are )?open|open windows?)\b", _F), ("window",)),
    ("pc_desktop", re.compile(r"\b(clipboard|copy to clipboard|notif(?:y|ication)|remind me on screen|volume|mute|unmute|"
                             r"turn (?:it )?(?:up|down)|open\s+(?:chrome|edge|notepad|the app|the file|the folder|calculator|"
                             r"explorer)|launch\s+)\b", _F), ("desktop",)),
    ("pc_screen", re.compile(r"\b(screenshot|screen ?shot|screen capture|capture (?:the|my) screen|what(?:'s| is) on (?:my |the )?screen|"
                            r"look at (?:my |the )?screen|see my screen|read the screen)\b", _F), ("screenshot", "analyze_image")),
    ("pc_click", re.compile(r"\b(click (?:the|on) [\w ]+ (?:button|link|menu|tab|icon)|press the [\w ]+ button|click [\"'][^\"']+[\"'])\b", _F),
     ("ui_click", "screenshot")),
    ("desktop", re.compile(r"\b(click|double[- ]?click|mouse|cursor|keyboard|type\s+(?:in|into)|press|hotkey|minimi[sz]e|"
                           r"window|desktop|screen)\b", _F), ("ui_click", "screenshot", "cua_mouse", "cua_keyboard", "cua_cursor")),
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
    """Every tool the model could use, searchable by keyword. Lives next to the loop so tool_search can add schemas.

    MCP tools (P4) ride along: `keywords` maps a tool name to extra words that should find it (its server's hint),
    `core` is the set of main tools of each server (they win ties), `more` returns a line about sibling tools."""

    def __init__(self, tools: Iterable[dict], keywords: Optional[Dict[str, str]] = None,
                 core: Optional[Iterable[str]] = None, more: Any = None, live: Any = None):
        self._by_name: Dict[str, dict] = {}
        for t in tools:
            name = (t.get("function") or {}).get("name")
            if name and name != TOOL_SEARCH_NAME:
                self._by_name[name] = t
        self._keywords = dict(keywords or {})
        self._core = set(core or ())
        self._more = more
        self._live = live                # P7: () -> schemas of tools that may appear mid-turn (Lucy's own new tools)

    def _refresh(self) -> None:
        if self._live is None:
            return
        try:
            for t in self._live() or ():
                name = (t.get("function") or {}).get("name")
                if name and name != TOOL_SEARCH_NAME and name not in self._by_name:
                    self._by_name[name] = t
        except Exception:                                # noqa: BLE001 - a lookup must never break the turn
            pass

    def names(self) -> List[str]:
        return list(self._by_name)

    def get(self, name: str) -> Optional[dict]:
        if name not in self._by_name:
            self._refresh()
        return self._by_name.get(name)

    def more_hint(self, found: Iterable[dict]) -> str:
        """Extra line for the tool_search answer (e.g. the other tools of an MCP server). Empty when there is none."""
        if self._more is None:
            return ""
        try:
            return str(self._more([t["function"]["name"] for t in found]) or "")
        except Exception:                                # noqa: BLE001 - a hint must never break a search
            return ""

    def search(self, query: str, limit: int = 4) -> List[dict]:
        self._refresh()
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
            extra = set(re.findall(r"[a-z0-9]+", self._keywords.get(name, "").lower()))
            for w in words:
                if w in name_words or w in name.lower():
                    scores[name] = scores.get(name, 0) + 3
                elif w in extra:
                    scores[name] = scores.get(name, 0) + 3
                elif w in desc:
                    scores[name] = scores.get(name, 0) + 1
            if name in self._core and name in scores:
                scores[name] += 2                        # a server's main tools beat its niche ones on a tie
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


# --- coding mode: the harness adds a short work-method note to the prompt while any of these tools is in play ---
CODING_TOOLS = frozenset({"code_search", "project_map", "apply_patch", "run_tests", "git", "format_and_lint"})

CODING_ADDENDUM = (
    "CODING MODE: work like this. (1) Look first: project_map for an unfamiliar project, code_search to find the exact place, "
    "read_local_file for just those lines. (2) Say your plan in update_plan if there are several steps. (3) Change code with "
    "apply_patch (several edits to one file at once) or edit_local_file; copy old text exactly. (4) The harness checks syntax after "
    "every code edit and adds the result to the tool answer: if it reports a problem, fix it before anything else. (5) Run run_tests "
    "after changing behaviour and fix what fails; do not claim it works until the tests pass. (6) Finish with a short report: what "
    "changed (file + what), test result, anything left. Never say you edited or tested something unless the tool answer confirms it."
)


def coding_mode(tool_names: Iterable[str]) -> bool:
    return bool(CODING_TOOLS & set(tool_names))


# --- Office mode: the harness adds the office guidance (.core/skills/definitions/office/SKILL.md) while a create / edit / render tool is in play ---
OFFICE_TOOLS = frozenset({"docx_create", "docx_edit", "xlsx_create", "xlsx_edit", "pptx_create", "pptx_edit", "pdf_create", "office_render"})

_OFFICE_DEFAULT = (
    "OFFICE FILES: never write library code; the tools build the file and send it to the chat. New file: docx_create / xlsx_create / "
    "pptx_create / pdf_create (easiest input: markdown; slides separated by a --- line). Change a file: office_read it first, then "
    "docx_edit / xlsx_edit / pptx_edit with exact text. Spreadsheets: headers in 'columns', calculations as formulas ('=B2*C2'). "
    "Slides: one idea each, at most 6 short bullets. If the answer says 'NOT sent to the chat yet', fix the problem and call the same tool "
    "again. Check the look with office_render + analyze_image. Do not write the file path or a MEDIA: line yourself.")
_guide_cache: Dict[str, Any] = {"mtime": None, "text": _OFFICE_DEFAULT}


def office_mode(tool_names: Iterable[str]) -> bool:
    return bool(OFFICE_TOOLS & set(tool_names))


def office_guidance(max_chars: int = 2600) -> str:
    """The text of .core/skills/definitions/office/SKILL.md (or .core/skills/office/SKILL.md) without its front matter (re-read when the file changes), else the built-in default."""
    try:
        from lucy import paths as _paths
        base = _paths.ROOT / ".core" / "skills"
        f = next(c for c in (base / "definitions" / "office" / "SKILL.md", base / "office" / "SKILL.md") if c.is_file())
        mtime = f.stat().st_mtime
        if _guide_cache["mtime"] != mtime:
            txt = f.read_text(encoding="utf-8")
            txt = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", txt, count=1, flags=re.DOTALL).strip()
            _guide_cache.update(mtime=mtime, text=txt[:max_chars] if txt else _OFFICE_DEFAULT)
        return _guide_cache["text"]
    except Exception:                                    # noqa: BLE001 - missing / unreadable file: built-in text
        return _OFFICE_DEFAULT
