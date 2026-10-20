"""Item 7 - citations. The harness (not the small model) collects the pages a web turn used and appends a numbered
'Sources' list to the answer. Sources = pages read with web_extract; if none was read, the top search results."""

from __future__ import annotations

import re
from urllib.parse import urlparse

_SEARCH_RE = re.compile(r"\[(\d+)\][ \t]+(.+?)\n[ \t]+URL:[ \t]+(\S+)", re.MULTILINE)
_EXTRACT_RE = re.compile(r"^=== (\S+) ===\n(.*?)(?=^=== \S+ ===\n|\Z)", re.MULTILINE | re.DOTALL)
_HEAD_RE = re.compile(r"^(?:Title:\s*(.+)|#\s+(.+))$", re.MULTILINE)
_BAD_BODY = ("[Failed", "[Error", "[Could not")


def _clean_url(u: str) -> str:
    u = u.strip().rstrip(".,;:'\"")
    return u if re.match(r"^https?://[^\s]+\.[^\s]+", u) else ""


def host(url: str) -> str:
    h = urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def parse_search(text: str) -> list[dict]:
    out = []
    for _n, title, url in _SEARCH_RE.findall(text or ""):
        u = _clean_url(url)
        if u:
            out.append({"url": u, "title": " ".join(title.split())})
    return out


def parse_extract(text: str) -> list[dict]:
    out = []
    for url, body in _EXTRACT_RE.findall(text or ""):
        u = _clean_url(url)
        if not u or body.lstrip().startswith(_BAD_BODY):
            continue
        m = _HEAD_RE.search(body)
        title = " ".join((m.group(1) or m.group(2) or "").split()) if m else ""
        out.append({"url": u, "title": title or host(u)})
    return out


def _md_title(t: str) -> str:
    return re.sub(r"[\[\]]", "", t)[:90].strip()


class SourceCollector:
    def __init__(self, max_sources: int = 6):
        self.max = max_sources
        self.read: list[dict] = []
        self.found: list[dict] = []
        self._titles: dict[str, str] = {}

    def add(self, tool: str, args: dict, result: str) -> None:
        if tool == "web_search":
            for s in parse_search(result):
                self._titles.setdefault(s["url"], s["title"])
                if all(s["url"] != f["url"] for f in self.found):
                    self.found.append(s)
        elif tool == "web_extract":
            for s in parse_extract(result):
                s["title"] = self._titles.get(s["url"]) or s["title"]
                if all(s["url"] != r["url"] for r in self.read):
                    self.read.append(s)

    def sources(self) -> list[dict]:
        chosen = self.read or self.found[:3]
        out = []
        for s in chosen[: self.max]:
            out.append({"n": len(out) + 1, "url": s["url"], "title": s["title"]})
        return out

    def footer(self, answer: str) -> str:
        """'' when nothing was read, the answer is tiny, or the model already linked these pages itself."""
        if len((answer or "").strip()) < 40:
            return ""
        src = [s for s in self.sources() if s["url"] not in (answer or "")]
        if not src:
            return ""
        lines = [f"{i}. [{_md_title(s['title']) or host(s['url'])}]({s['url'].replace(')', '%29')}) — {host(s['url'])}"
                 for i, s in enumerate(src, 1)]
        return "\n\n**Sources**\n" + "\n".join(lines)
