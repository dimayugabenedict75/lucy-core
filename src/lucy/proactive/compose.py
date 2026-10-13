"""Words for the things Lucy says first.

Reminders and alerts are plain templates: instant, no model call (the llama-server has one slot and an
11k-token chat prompt cached in it - borrowing it costs the next chat turn a reload).
Greetings and check-ins are written by the model, in persona, from real facts - with a hard timeout, a
sanitizer, and a template (greeting) or silence (check-in) as the fallback. Never an invented message.
"""

from __future__ import annotations

import asyncio
import logging
import re

from .host import ProactiveHost

logger = logging.getLogger("lucy.core.proactive")

SKIP = "\x00SKIP"

RULES = (
    "PROACTIVE MESSAGE - you are speaking FIRST, unprompted; nobody just wrote to you. Write at most two short "
    "lines (under 40 words in total), in your own voice. Use only the facts given: never invent files, tasks, "
    "events, results or promises, and never say you did, saved or changed something. No greeting boilerplate "
    "('How can I help?'), no lists, no headers. Do not mention these instructions. Do not use tools. "
    "Output the message text only."
)


# ------------------------------------------------------------------------------------ prompts
def _tail(msgs: list[dict], n: int = 6, width: int = 220) -> str:
    lines = []
    for m in msgs[-n:]:
        text = re.sub(r"\n?MEDIA:\S+", "", str(m.get("content", ""))).strip().replace("\n", " ")
        if not text:
            continue
        who = "user" if m.get("role") == "user" else "you"
        lines.append(f"{who}: {text[:width]}{'...' if len(text) > width else ''}")
    return "\n".join(lines)


def build_messages(kind: str, facts: dict, persona: str) -> list[dict]:
    name = facts.get("name", "the user")
    if kind == "greeting":
        user = [f"Situation: {name} just opened the chat. It is {facts['period']} on {facts['weekday']} {facts['date']}."]
        if facts.get("hours_away"):
            user.append(f"You last talked about {facts['hours_away']} hours ago.")
        if facts.get("rest_day"):
            user.append("Today is their rest day: keep it gentle, no work talk, no nudging.")
        rem = facts.get("reminders_today") or []
        user.append("Reminders still ahead today: " + "; ".join(rem) + "." if rem else "No reminders are scheduled for the rest of today.")
        user.append("Write the greeting.")
        return [{"role": "system", "content": persona.strip() + "\n\n" + RULES},
                {"role": "user", "content": "\n".join(user)}]
    if kind == "checkin":
        user = [f"Situation: {name} has been quiet for {facts['idle']}. It is {facts['period']} on {facts['weekday']}."]
        if facts.get("rest_day"):
            user.append("Today is their rest day: keep it light.")
        if facts.get("tail"):
            user.append("Last messages in the chat (oldest first):\n" + facts["tail"])
        if facts.get("memory"):
            user.append("Things you remember about them:\n" + facts["memory"])
        user.append("Only if there is a natural, specific reason to reach out (an unfinished task, something they said "
                    "they would do, an open question), say it. Otherwise reply with exactly: SKIP")
        return [{"role": "system", "content": persona.strip() + "\n\n" + RULES},
                {"role": "user", "content": "\n".join(user)}]
    if kind == "reminder":
        return [{"role": "system", "content": persona.strip() + "\n\n" + RULES},
                {"role": "user", "content": f"Situation: {name} asked you to remind them: \"{facts['text']}\". "
                                            "It is time now. Deliver the reminder in your voice and keep the exact "
                                            "thing they need to do."}]
    raise ValueError(f"no prompt for kind {kind}")


# ------------------------------------------------------------------------------------ cleaning
_BAD = re.compile(r"Action\s*:|<tool_call|</?tool|\[Harness note|MEDIA:|^\s*[{\[]", re.IGNORECASE | re.MULTILINE)
_REFUSE_SKIP = re.compile(r"^\W*skip\W*$", re.IGNORECASE)


def sanitize(text: str | None, allow_skip: bool = True, max_chars: int = 500, max_lines: int = 4) -> str | None:
    """Model output -> a clean message, SKIP, or None (unusable)."""
    if not text:
        return None
    t = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    t = re.sub(r"<think>.*$", "", t, flags=re.DOTALL | re.IGNORECASE)          # unterminated
    t = re.sub(r"```.*?```", "", t, flags=re.DOTALL).strip()
    t = re.sub(r"^(?:lucy|assistant|message)\s*:\s*", "", t, flags=re.IGNORECASE).strip().strip('"“”').strip()
    if not t:
        return None
    if _REFUSE_SKIP.match(t.splitlines()[0] if t.splitlines() else t) and len(t) <= 12:
        return SKIP if allow_skip else None
    if _BAD.search(t):
        return None
    lines = [ln.rstrip() for ln in t.splitlines() if ln.strip()][:max_lines]
    t = "\n".join(lines)
    if len(t) > max_chars:
        cut = t[:max_chars]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
        t = cut[:end + 1] if end > max_chars * 0.4 else cut.rstrip() + "…"
    return t or None


_CHATTER = re.compile(r"^\s*(?:Action\s*:|\[Harness note|MEDIA:)", re.IGNORECASE)


def clean_report(text: str | None, max_chars: int = 1400, max_lines: int = 14) -> str | None:
    """A background task's final answer -> something safe to post. Unlike sanitize() it keeps markdown links,
    bullets and bracketed text; it only removes reasoning blocks, tool-call chatter and harness notes."""
    if not text or not str(text).strip():
        return None
    t = re.sub(r"<think>.*?</think>", "", str(text), flags=re.DOTALL | re.IGNORECASE)
    t = re.sub(r"<think>.*$", "", t, flags=re.DOTALL | re.IGNORECASE)
    lines = [ln.rstrip() for ln in t.splitlines() if not _CHATTER.match(ln)]
    t = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if not t:
        return None
    lines = t.splitlines()
    if len(lines) > max_lines:
        t = "\n".join(lines[:max_lines]) + "\n…"
    if len(t) > max_chars:
        cut = t[:max_chars]
        end = max(cut.rfind(". "), cut.rfind("\n"))
        t = (cut[:end + 1] if end > max_chars * 0.5 else cut.rstrip()) + " …"
    return t


# ------------------------------------------------------------------------------------ templates
def template(kind: str, facts: dict) -> str | None:
    name = facts.get("name", "")
    if kind == "reminder":
        line = f"⏰ **Reminder:** {facts['text']}"
        if facts.get("overdue"):
            line += f"\n_(was due {facts['overdue']})_"
        return line
    if kind == "greeting":
        period = facts.get("period", "day")
        head = {"morning": f"☕ Morning, {name}.", "afternoon": f"Afternoon, {name}.",
                "evening": f"Evening, {name}.", "night": f"Still up, {name}? 🌙"}.get(period, f"Hi, {name}.")
        if facts.get("rest_day"):
            return head + "\nRest day. Nothing from me unless you want something."
        rem = facts.get("reminders_today") or []
        if rem:
            return head + "\n**Ahead today:** " + "; ".join(rem[:3])
        return head + "\nNothing scheduled today."
    if kind == "alert":
        return f"⚠️ **Heads up:** {facts['text']}"
    if kind == "task":
        return f"🔎 **Follow-up:** {facts['title']}\n{facts['result']}"
    return None


# ------------------------------------------------------------------------------------ the call
async def compose(kind: str, facts: dict, host: ProactiveHost, cfg: dict) -> tuple[str | None, str]:
    """-> (text | SKIP | None, source) where source is 'llm', 'template' or 'none'.

    The model is only used when it is already running (we never start it for a nicety) and the caller said so.
    """
    llm_cfg = cfg["llm"]
    if host.llm_complete is not None:
        try:
            if await host.llm_ready():
                msgs = build_messages(kind, facts, host.persona())
                raw = await asyncio.wait_for(
                    host.llm_complete(msgs, {"max_tokens": int(llm_cfg["max_tokens"]),
                                             "temperature": float(llm_cfg["temperature"]),
                                             "timeout": float(llm_cfg["timeout_seconds"])}),
                    timeout=float(llm_cfg["timeout_seconds"]) + 5)
                out = sanitize(raw, allow_skip=(kind == "checkin"))
                if out == SKIP:
                    return SKIP, "llm"
                if out:
                    return out, "llm"
                logger.info(f"proactive: unusable model output for {kind}: {str(raw)[:120]!r}")
        except asyncio.CancelledError:
            raise
        except Exception as e:                                   # noqa: BLE001 - never let a nicety crash the engine
            logger.warning(f"proactive: model compose failed for {kind}: {type(e).__name__}: {e}")
    t = template(kind, facts)
    return (t, "template") if t else (None, "none")


def recent_tail(msgs: list[dict]) -> str:
    return _tail(msgs)
