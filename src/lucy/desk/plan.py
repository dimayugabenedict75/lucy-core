"""Item 5 - plan mode: the agent looks things up and writes a plan, nothing is changed until the user approves.

How it works: the turn runs with READ-ONLY tools (+ a few lookup tools), no update_plan checklist (it would push the
loop to execute) and a system note. The UI shows the reply with Approve / Change buttons; Approve sends APPROVE_TEXT as the
next (normal) message. Because the plan turn cannot write, no approval cards are needed for it, and P1 permissions
still apply to the execution turn exactly as always.
"""

from __future__ import annotations

PLAN_NOTE = (
    "PLAN MODE: the user wants to approve a plan BEFORE anything is changed. Right now you can only look things up. "
    "Use your read-only tools if you need facts (read a file, search). Then answer with the plan: a short numbered list "
    "(at most 8 steps), each step one concrete action (which file or tool, what change). Finish with one line: "
    "'Approve the plan to run it, or tell me what to change.' Do NOT carry out any step now and do not say you did.")

APPROVE_TEXT = "Plan approved. Carry it out now, step by step, and tell me when it is done."

# Lookup tools that are not "read" tier but only fetch information.
LOOKUP_EXTRA = frozenset({"web_search", "web_extract", "recall_memory", "search_conversations"})
NEVER = frozenset({"update_plan", "finish_task", "tool_search", "task", "core_change", "user_tool"})


def filter_tools(tools: list[dict], tier_of) -> list[dict]:
    """Keep read-tier tools and the lookup extras. `tier_of(name)` -> tier or None (unknown tools are dropped)."""
    keep = []
    for t in tools:
        name = (t.get("function") or {}).get("name", "")
        if name in NEVER:
            continue
        if name in LOOKUP_EXTRA or tier_of(name) == "read":
            keep.append(t)
    return keep


def with_note(messages: list[dict]) -> list[dict]:
    """Insert PLAN_NOTE after the system blocks (before the history), like the coding addendum."""
    msgs = list(messages)
    at = next((i for i, m in enumerate(msgs) if m.get("role") != "system"), len(msgs))
    msgs.insert(at, {"role": "system", "content": PLAN_NOTE})
    return msgs
