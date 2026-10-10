"""
Search past conversations (all sessions) in sessions.sqlite.

Uses an SQLite FTS5 index kept in the same database (table `messages_fts`). The index is *not*
maintained by triggers: before each search, any messages added since the last search are indexed
(incremental, cheap), and results are joined back to `messages` so deleted sessions never show up.
If FTS5 isn't available, or finds nothing, it falls back to a plain substring scan.
"""

from __future__ import annotations

import re
import sqlite3

_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "it", "we", "i", "you", "my",
         "me", "that", "this", "was", "were", "about", "what", "did", "do", "when", "how", "with", "at",
         "by", "be", "are", "have", "had", "has", "our", "your", "talked", "said", "last", "time"}


def _terms(query: str) -> list[str]:
    ts = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 1 and t not in _STOP]
    return ts or [t for t in re.findall(r"\w+", query.lower()) if t]


def _ensure_index(conn: sqlite3.Connection) -> bool:
    """Create/sync the FTS index. Returns False if FTS5 is unavailable."""
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
                     "USING fts5(content, session_id UNINDEXED, tokenize='unicode61')")
        conn.execute(
            "INSERT INTO messages_fts(rowid, content, session_id) "
            "SELECT rowid_ref, content, session_id FROM messages "
            "WHERE rowid_ref > COALESCE((SELECT MAX(rowid) FROM messages_fts), 0) "
            "AND role IN ('user', 'assistant')")
        conn.commit()
        return True
    except sqlite3.OperationalError:
        return False


def _snippet(text: str, terms: list[str], width: int = 420) -> str:
    text = " ".join(text.split())
    if len(text) <= width:
        return text
    low, pos = text.lower(), -1
    for t in terms:
        pos = low.find(t)
        if pos != -1:
            break
    start = max(0, (pos if pos != -1 else 0) - width // 3)
    end = min(len(text), start + width)
    return ("..." if start else "") + text[start:end] + ("..." if end < len(text) else "")


_SELECT = ("SELECT m.rowid_ref AS id, m.session_id, m.role, m.content, m.created_at, s.title "
           "FROM messages m LEFT JOIN sessions s ON s.session_id = m.session_id ")


_SELECT_FTS = ("SELECT m.rowid_ref AS id, m.session_id, m.role, m.content, m.created_at, s.title "
               "FROM messages_fts JOIN messages m ON m.rowid_ref = messages_fts.rowid "
               "LEFT JOIN sessions s ON s.session_id = m.session_id ")


def _row(r) -> dict:
    return {"id": r[0], "session_id": r[1], "role": r[2], "content": r[3], "date": r[4], "title": r[5]}


def search_history(conn, lock, query: str, *, session_id: str | None = None,
                   exclude_session: str | None = None, days: int | None = None, limit: int = 5) -> list[dict]:
    """Messages matching `query`, best first. Each: session_id, title, role, date, snippet."""
    terms = _terms(query)
    if not terms:
        return []
    limit = max(1, min(int(limit), 15))
    extra, params_extra = "", []
    if session_id:
        extra += " AND m.session_id = ?"; params_extra.append(session_id)
    if exclude_session:
        extra += " AND m.session_id != ?"; params_extra.append(exclude_session)
    if days:
        extra += " AND m.created_at >= datetime('now', ?, 'localtime')"; params_extra.append(f"-{int(days)} days")
    extra += " AND m.role IN ('user', 'assistant')"

    rows, partial = [], False
    with lock:
        if _ensure_index(conn):
            for joiner in (" AND ", " OR "):
                match = joiner.join(f'"{t}"*' for t in terms)
                try:
                    rows = conn.execute(
                        _SELECT_FTS + "WHERE messages_fts MATCH ?" + extra
                        + " ORDER BY bm25(messages_fts), m.rowid_ref DESC LIMIT ?",
                        [match, *params_extra, limit]).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                if rows:
                    partial = joiner == " OR " and len(terms) > 1
                    break
        if not rows:                                   # no FTS5, or e.g. Japanese text with no word breaks
            like = " AND ".join("m.content LIKE ?" for _ in terms)
            rows = conn.execute(_SELECT + "WHERE " + like + extra + " ORDER BY m.rowid_ref DESC LIMIT ?",
                                [*(f"%{t}%" for t in terms), *params_extra, limit]).fetchall()
    out = []
    for r in rows:
        d = _row(r)
        d["snippet"] = _snippet(d.pop("content"), terms)
        d["partial"] = partial
        out.append(d)
    return out


def read_session(conn, lock, session_id: str, limit: int = 12) -> list[dict]:
    """The most recent `limit` messages of one session, oldest first."""
    with lock:
        rows = conn.execute(
            _SELECT + "WHERE m.session_id = ? AND m.role IN ('user', 'assistant') "
            "ORDER BY m.rowid_ref DESC LIMIT ?", (session_id, max(1, min(int(limit), 30)))).fetchall()
    out = []
    for r in reversed(rows):
        d = _row(r)
        d["snippet"] = _snippet(d.pop("content"), [], 500)
        out.append(d)
    return out


def format_results(results: list[dict], header: str) -> str:
    if not results:
        return ""
    if any(r.get("partial") for r in results):
        header += " (no message contained ALL the words; these match only some of them - check they are really relevant)"
    lines = [header]
    for i, r in enumerate(results, 1):
        title = f' "{r["title"]}"' if r.get("title") else ""
        lines.append(f'[{i}] {r["date"]} -{title} (session {r["session_id"]}) {r["role"]}: {r["snippet"]}')
    text = "\n".join(lines)
    return text if len(text) <= 5000 else text[:5000] + "\n[... truncated ...]"
