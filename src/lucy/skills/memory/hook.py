"""
Memory Hook Skill for Lucy Core.

Watches user messages for memory-worthy patterns, then appends
an inline confirmation UI ("Save to memory? [Yes] [No]") to the next
assistant response. Only persists on explicit user click.

Triggers (configurable):
  - "my name is"
  - "call me"
  - "i like"
  - "i prefer"
  - "i always want"
  - "remember that"
  - "i use"
"""

import re
import json
from pathlib import Path
from urllib.parse import quote_plus

# --- Configurable trigger phrases ---
DEFAULT_TRIGGERS = [
    r"my name is",
    r"call me",
    r"\bi like\b",
    r"\bi prefer\b",
    r"\bi always want\b",
    r"remember that",
    r"\bi use\b",
]

_TRIGGER_PATTERNS = [re.compile(p, re.IGNORECASE) for p in DEFAULT_TRIGGERS]

# Where this skill lives
SKILL_DIR = Path(__file__).parent
TRIGGER_FILE = SKILL_DIR / "triggers.yaml"


def _load_triggers():
    """Load trigger patterns from file, fallback to defaults."""
    if TRIGGER_FILE.exists():
        try:
            import yaml
            with open(TRIGGER_FILE) as f:
                cfg = yaml.safe_load(f) or {}
            patterns = cfg.get("triggers", DEFAULT_TRIGGERS)
            return [re.compile(p, re.IGNORECASE) for p in patterns]
        except Exception:
            pass
    return _TRIGGER_PATTERNS


def detect_memory_candidate(text: str) -> str | None:
    """
    Check if the user message contains a memory-worthy fact.
    Returns the matched phrase + sentence if found, None otherwise.
    """
    patterns = _load_triggers()
    for pattern in patterns:
        if match := pattern.search(text):
            # Return the sentence containing the match
            sentences = re.split(r'[.!?]+', text)
            for sent in sentences:
                if pattern.search(sent):
                    return sent.strip()
            return text.strip()
    return None


def render_confirmation(fact: str, session_id: str) -> str:
    """
    Generate the inline UI buttons for saving to memory.
    The Yes button links to /api/memory/save?fact=...&session=...
    """
    encoded_fact = quote_plus(fact)
    encoded_session = quote_plus(session_id)
    yes_url = f"/api/memory/save?fact={encoded_fact}&session={encoded_session}"

    return (
        f'<div class="memory-prompt" style="margin-top: 8px; padding: 4px 0;">'
        f'<span style="font-size: 12px; color: #666;">Save to memory? </span>'
        f'<a href="{yes_url}" '
        f'  style="color: #00E5FF; text-decoration: none; '
        f'         border: 1px solid #00E5FF; padding: 2px 8px; '
        f'         border-radius: 4px; font-size: 12px;">'
        f'Yes</a>'
        f'<button onclick="this.closest(\'.memory-prompt\').remove()" '
        f'  style="margin-left: 4px; background: none; border: 1px solid #444; '
        f'         padding: 2px 8px; border-radius: 4px; '
        f'         cursor: pointer; font-size: 12px;">No</button>'
        f'</div>'
    )
