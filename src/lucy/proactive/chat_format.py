"""Keep stored history valid for strict chat templates once Lucy can speak first.

Lucy's models (Gemma-style 4B, Mistral-style 12B, run with --jinja) require the conversation to alternate
user / assistant. A reminder or greeting is an assistant message that nobody asked for, so the stored
history can now contain two assistant messages in a row, or start with one. This repairs the list that is
sent to the model (the stored history is never changed):

  * consecutive messages with the same role are merged (joined by a blank line)
  * a history that starts with an assistant message gets a short stand-in user line in front of it,
    or - when the history was cut from the middle of a longer chat - that leading message is dropped
"""

from __future__ import annotations

from typing import Iterable

LEAD_PLACEHOLDER = "(Lucy spoke first.)"


def normalize_history(msgs: Iterable[dict], truncated: bool = False) -> list[dict]:
    out: list[dict] = []
    for m in msgs:
        role, content = m.get("role"), m.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            out.append(dict(m))                      # system / tool / multimodal: leave untouched
            continue
        if out and out[-1].get("role") == role and isinstance(out[-1].get("content"), str):
            out[-1]["content"] = out[-1]["content"].rstrip() + "\n\n" + content.lstrip()
            continue
        out.append(dict(m))
    first = next((i for i, m in enumerate(out) if m.get("role") in ("user", "assistant")), None)
    if first is not None and out[first].get("role") == "assistant":
        if truncated:
            del out[first]
            while first < len(out) and out[first].get("role") == "assistant":
                del out[first]
        else:
            out.insert(first, {"role": "user", "content": LEAD_PLACEHOLDER})
    return out
