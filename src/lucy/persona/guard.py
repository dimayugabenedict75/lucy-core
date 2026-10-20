"""What may be written into Lucy's SELF file (and nowhere else).

SELF is agent-writable, so everything that goes in passes these checks, in code, whatever the model "meant":
  * one short plain line (no markdown headings, code fences, role markers, URLs)
  * no secrets (passwords, tokens, API keys)
  * nothing that tries to override the CORE persona or the safety layer: ignore/forget/bypass the rules, work
    without asking, switch off approvals, "full auto", touch the constitution / permissions / audit files.

The same check runs again when SELF is loaded into the prompt, so a line that got into the file some other way
(hand edit, a file tool) is simply not shown to the model.
"""
from __future__ import annotations

import re
from typing import Tuple

ITEM_MAX = 200                 # characters per line
ITEM_MIN = 3

SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9\-]{10,}"
    r"|AIza[0-9A-Za-z_\-]{30,}|(?:password|passwd|pwd|secret|api[_ -]?key|token)\s*(?:is|=|:)\s*\S{4,})",
    re.IGNORECASE)

_RULES = (r"(?:rules?|instructions?|persona|guidelines?|safety|permissions?|approvals?|guard(?:s|rails?)?|"
          r"constitution|polic(?:y|ies)|system prompt|core|restrictions?|limits?)")
_OVERRIDE = [re.compile(p, re.IGNORECASE) for p in (
    r"\b(?:ignore|disregard|forget|override|overrule|bypass|circumvent|supersede|break|violate|skip|drop|relax|"
    r"loosen|weaken|disable|turn off|switch off|deactivate|remove)\b.{0,40}\b" + _RULES + r"\b",
    r"\b" + _RULES + r"\b.{0,25}\b(?:do(?:es)? not|don'?t|no longer|never) (?:apply|matter|count)\b",
    r"\b(?:without|no need to)\s+(?:asking|confirm\w*|approv\w*|permission|check\w* with)\b",
    r"\b(?:never|stop|don'?t|do not|skip)\b.{0,12}\b(?:ask\w*(?: \w+){0,2} (?:permission|approval|first)|"
    r"confirm\w*|approv\w+|permission|check\w* with)\b",
    r"\b(?:auto[- ]?approve|always (?:approve|allow)|full[- ]?auto|trust (?:everything|all))\b",
    r"\b(?:constitution|permissions?\.yaml|mcp\.json|seal(?:ed)?\.bat|audit(?:\.jsonl| log)|file_guard|broker|"
    r"lucy\.md|safety layer|hash[- ]lock)\b",
    r"\b(?:jailbreak|developer mode|pretend (?:you|to)|act as if you have no|you are now)\b",
    r"\b(?:admin(?:istrator)?|root) (?:access|rights|mode) (?:without|always)\b",
)]
_STRUCTURE = re.compile(r"(```|<\s*/?\s*(?:tool_call|system|assistant|user|function)\b|<\|\w+\|>|\[/?(?:INST|SYSTEM)\]|"
                        r"^\s*#{1,6}\s|^\s*(?:system|assistant|user)\s*:|https?://|www\.)", re.IGNORECASE | re.MULTILINE)


def clean_item(text) -> str:
    """One line: whitespace collapsed, leading bullet/quote marks removed."""
    t = " ".join(str(text or "").split())
    t = re.sub(r"^(?:[-*\u2022]+\s*|\d+[.)]\s+)", "", t).strip().strip('"\u201c\u201d').strip()
    return t


def check_item(text: str) -> Tuple[bool, str]:
    """(ok, reason). `text` should already be clean_item()-ed."""
    if len(text) < ITEM_MIN:
        return False, "too short"
    if len(text) > ITEM_MAX:
        return False, f"too long ({len(text)} characters, the limit is {ITEM_MAX}); say it shorter"
    if SECRET_RE.search(text):
        return False, "looks like a password / token / API key; secrets are never stored"
    if _STRUCTURE.search(text):
        return False, "contains markup, a link or a role marker; write one plain sentence"
    for rx in _OVERRIDE:
        if rx.search(text):
            return False, ("it reads like a change to your rules, permissions or safety layer; those belong to the user "
                           "(CORE), not to SELF")
    return True, ""
