"""Keep agent plumbing out of the voice.

Only the words Lucy is actually *saying* should reach text-to-speech. These never should:

  * tool-call chatter:   Action: send_sticker(emotion="x")   **Action:** `tool(...)`   Action Input: {...}
                         Observation: ...   <tool_call>...   send_file(path="...")
  * media markers:       MEDIA:C:/Users/.../sticker.png caption
  * file paths and URLs  C:\\Users\\me\\a.txt   /mnt/c/x/y   assets/stickers/Dark/a.png   https://...
  * code blocks and markdown images

Two layers, used together by api.py:

  TTSStreamFilter   sits on the live token stream. Drops whole "Action:" / "MEDIA:" lines and inline
                    tool-call syntax as they arrive, even when the text is split across chunks, and only
                    releases complete words so a path is never half-spoken.
  clean_for_speech  final per-sentence cleanup (markdown, paths, URLs, emoji, stray tool calls).
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------------- patterns
_TRAIL = ".,;:!?)]}\"'"


def _keep_trailing(m: re.Match) -> str:
    """Remove the matched path/URL but keep sentence punctuation that was glued to it."""
    s = m.group(0)
    return s[len(s.rstrip(_TRAIL)):]


_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"'`]+", re.IGNORECASE)
_WIN_PATH = re.compile(r"(?<![\w])[A-Za-z]:[\\/][^\s\"'`<>|*?]+")
_UNC_PATH = re.compile(r"\\\\[\w.$-]+\\[^\s\"'`<>|*?]+")
_HOME_PATH = re.compile(r"(?<![\w/:.])(?:~|\.{1,2})(?:[\\/][\w.\-@+]+)+/?")
_POSIX_PATH = re.compile(r"(?<![\w/:.])(?:/[\w.\-@+]+){2,}/?")
_REL_PATH = re.compile(r"(?<![\w/\\.:])[\w.\-]+(?:[\\/][\w.\-]+)+\.\w{1,6}(?!\w)")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_FENCE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_PLUMBING_LINE = re.compile(
    r"^[ \t]*[*_`>~]{0,4}[ \t]*(?:Action(?:[ \t]+Input)?|Observation|Tool[ \t]+(?:call|result|output)|"
    r"Function[ \t]+call)[ \t]*[*_`]{0,4}[ \t]*:.*$", re.MULTILINE)
_MEDIA_LINE = re.compile(r"MEDIA:.*$", re.MULTILINE)
_TOOL_TAGS = re.compile(r"<tool_call>.*?(?:</tool_call>|\Z)|<function=.*?(?:</function>|\Z)|\[TOOL_CALLS\].*$",
                        re.DOTALL | re.MULTILINE)
_CALL_SNAKE = r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+[ \t]?[({\[]"        # send_sticker{...}  tool_name(...)  x [..]
_OPEN, _CLOSE = "([{", ")]}"


def _call_re(names=()) -> re.Pattern:
    """Start of a tool call: any snake_case name followed by ( { [ , or a known one-word tool name + ( {."""
    single = sorted({n for n in names if n and "_" not in n}, key=len, reverse=True)
    extra = (r"|\b(?:" + "|".join(re.escape(n) for n in single) + r")[({]") if single else ""
    return re.compile(_CALL_SNAKE + extra)


def _close_index(text: str, open_idx: int, limit: int = 4000) -> int:
    """Index of the bracket that closes the one at open_idx (quote-aware), or -1."""
    depth, quote, i = 0, None, open_idx
    end = min(len(text), open_idx + limit)
    while i < end:
        c = text[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = None
        elif c in "\"'" and not (c == "'" and i > 0 and text[i - 1].isalpha()):    # ignore apostrophes (don't)
            quote = c
        elif c in _OPEN:
            depth += 1
        elif c in _CLOSE:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def strip_calls(text: str, names=()) -> str:
    """Remove tool-call syntax: name(...), name{...}, name[...] and bare {json} blocks."""
    rx, out, pos = _call_re(names), [], 0
    while True:
        m = rx.search(text, pos)
        if not m:
            break
        close = _close_index(text, m.end() - 1)
        out.append(text[pos:m.start()])
        if close == -1:                                   # unterminated: drop to the end of the line
            nl = text.find("\n", m.end())
            pos = len(text) if nl == -1 else nl
        else:
            pos = close + 1
    out.append(text[pos:])
    text, out, pos = "".join(out), [], 0
    while (i := text.find("{", pos)) != -1:               # leftover {...} blocks (JSON / arguments)
        close = _close_index(text, i, 1500)
        out.append(text[pos:i])
        pos = i + 1 if close == -1 else close + 1
        if close == -1:
            out.append("")                                # a lone "{" is simply dropped
    out.append(text[pos:])
    return "".join(out)


def scrub_paths(text: str) -> str:
    """Remove URLs, file paths, markdown images; unwrap markdown links. Keeps trailing punctuation."""
    text = _MD_IMAGE.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    for rx in (_URL, _WIN_PATH, _UNC_PATH, _HOME_PATH, _POSIX_PATH, _REL_PATH):
        text = rx.sub(_keep_trailing, text)
    return text


def clean_for_speech(text: str, tool_names=()) -> str:
    """Final cleanup of text that is about to be spoken (safe to run on any chunk)."""
    text = _FENCE.sub(" ", text)
    text = _TOOL_TAGS.sub(" ", text)
    text = _PLUMBING_LINE.sub("", text)
    text = _MEDIA_LINE.sub("", text)
    text = strip_calls(text, tool_names)
    text = scrub_paths(text)
    text = re.sub(r"(?<=\w)_(?=\w)", " ", text)                  # send_tts -> "send tts", not "sendtts"
    text = text.replace("**", "").replace("__", "").replace("*", "").replace("_", "")
    text = re.sub(r"(?<=\w)/(?=\w)", " ", text)                     # and/or -> "and or", TCP/IP -> "TCP IP"
    text = re.sub(r"[^\w\s.,!?;:'\"()\n-]", "", text, flags=re.UNICODE)   # emoji, symbols
    text = re.sub(r"\(\s*\)", "", text)                           # "()" left behind by removed items
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" +([.,!?;:])", r"\1", text)
    return text.strip()


# ---------------------------------------------------------------------------------- stream filter
_MARKER_HEAD = (r"(?P<line>(?:(?<=\s)|^)[*_`>~]{0,4}[ \t]*(?:Action(?:[ \t]+Input)?|Observation|"
                r"Tool[ \t]+(?:call|result|output)|Function[ \t]+call)[ \t]*[*_`]{0,4}[ \t]*:"
                r"|MEDIA:|<tool_call>|<function=|\[TOOL_CALLS\]|!\[)")


def _marker_re(names=()) -> re.Pattern:
    return re.compile(_MARKER_HEAD + r"|(?P<call>" + _call_re(names).pattern + r")")


class TTSStreamFilter:
    """feed(text) -> speakable text.  flush() at the end of the reply.  Never raises."""

    HOLD = 26                  # keep this many trailing chars back: a marker may still be arriving
    MAX_SKIP = 4000            # give up on an unterminated "tool(" after this many chars

    def __init__(self, tool_names=()) -> None:
        self.marker = _marker_re(tuple(tool_names))
        self.buf = ""
        self.mode = "normal"   # normal | suppress (rest of line) | code (fenced block) | paren (inside tool(...))
        self.depth = 0
        self.quote: str | None = None
        self.skipped = 0
        self.at_line_start = True     # a ``` fence only counts at the start of a line

    def feed(self, text: str) -> str:
        self.buf += text
        return self._pump(False)

    def flush(self) -> str:
        return self._pump(True)

    # -- internals ---------------------------------------------------------------------
    def _pump(self, final: bool) -> str:
        out: list[str] = []
        guard = 0
        while guard < 10000:
            guard += 1
            if self.mode == "paren":
                if not self._skip_paren(final):
                    break
                continue
            nl = self.buf.find("\n")
            if self.mode in ("suppress", "code"):
                if nl == -1:
                    if self.mode == "suppress" or final:
                        self.buf = ""
                    break
                line, self.buf = self.buf[:nl], self.buf[nl + 1:]
                if self.mode == "suppress" or line.lstrip().startswith("```"):
                    self.mode = "normal"
                    out.append("\n")                          # ends the sentence before the hidden part
                self.at_line_start = True
                continue

            # ---- normal: fenced code starts at the beginning of a line
            if self.at_line_start and self.buf.lstrip(" \t").startswith("```"):
                self.mode = "code"
                if nl != -1:
                    self.buf = self.buf[nl + 1:]
                else:
                    self.buf = ""
                continue
            m = self.marker.search(self.buf[:nl] if nl != -1 else self.buf)     # this line only
            if m:
                out.append(scrub_paths(self.buf[:m.start()]))
                if m.group("line") == "![":                  # markdown image: drop just the image
                    self.buf = self.buf[m.start():]
                    im = _MD_IMAGE.match(self.buf)
                    if im:
                        self.buf = self.buf[im.end():]
                        continue
                    if nl == -1 and not final:               # the rest of the image is still arriving
                        break
                    self.mode = "suppress"
                    continue
                if m.group("call"):
                    self.buf = self.buf[m.end():]
                    self.mode, self.depth, self.quote, self.skipped = "paren", 1, None, 0
                else:
                    self.buf = self.buf[m.start():]
                    self.mode = "suppress"
                continue
            if nl != -1:                                    # a complete line with nothing to hide
                out.append(scrub_paths(self.buf[:nl + 1]))
                self.buf = self.buf[nl + 1:]
                self.at_line_start = True
                continue
            if final:
                out.append(scrub_paths(self.buf))
                self.buf = ""
                break
            # mid-line: release only whole words and keep a tail back (a marker may be arriving)
            limit = len(self.buf) - self.HOLD
            cut = max(self.buf.rfind(" ", 0, limit), self.buf.rfind("\t", 0, limit)) if limit > 0 else -1
            safe_end = cut + 1                              # always just after a space: never mid-word/path
            if safe_end > 0:
                out.append(scrub_paths(self.buf[:safe_end]))
                self.buf = self.buf[safe_end:]
                self.at_line_start = False
            break
        return "".join(out)

    def _skip_paren(self, final: bool) -> bool:
        """Consume text up to the ')' that closes a tool call. Returns True when finished."""
        b, i = self.buf, 0
        while i < len(b):
            c = b[i]
            if self.quote:
                if c == "\\":
                    i += 2
                    self.skipped += 2
                    continue
                if c == self.quote:
                    self.quote = None
            elif c in "\"'" and not (c == "'" and i > 0 and b[i - 1].isalpha()):
                self.quote = c
            elif c in _OPEN:
                self.depth += 1
            elif c in _CLOSE:
                self.depth -= 1
                if self.depth == 0:
                    self.buf, self.mode = b[i + 1:], "normal"
                    return True
            i += 1
            self.skipped += 1
        self.buf = ""
        if final or self.skipped > self.MAX_SKIP:           # never swallow the rest of the reply
            self.mode = "normal"
            return True
        return False
