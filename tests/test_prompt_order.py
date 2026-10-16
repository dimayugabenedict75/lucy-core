"""Prompt order + tool-list stability (KV-cache reuse). Same stubs as test_tool_registry_api.py (no GPU/Windows needed)."""
import os, shutil, sys, tempfile, types
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="lucy_po_"))
os.environ.setdefault("LUCY_ROOT", str(TMP))
os.environ.setdefault("LUCY_SAFE_ROOT", str(TMP))
(TMP / ".core" / "personas").mkdir(parents=True, exist_ok=True)
(TMP / "ui").mkdir(exist_ok=True)
(TMP / "ui" / "chat.html").write_text("<html><body>key=__API_KEY_PLACEHOLDER__</body></html>")
shutil.copy(HERE / ".core" / "personas" / "lucy.md", TMP / ".core" / "personas" / "lucy.md")
sys.path.insert(0, str(HERE / "src"))
tk = types.ModuleType("tiktoken")
tk.get_encoding = lambda name: types.SimpleNamespace(encode=lambda t: t.split())
sys.modules.setdefault("tiktoken", tk)
if "lucy.cua.lucy_cua" not in sys.modules:
    cua = types.ModuleType("lucy.cua.lucy_cua")
    for n in ("get_cursor_pos", "set_cursor_pos", "mouse_click", "mouse_drag", "key_combo", "type_text", "minimize_all"):
        setattr(cua, n, lambda *a, **k: {})
    sys.modules["lucy.cua.lucy_cua"] = cua

from lucy.server import api  # noqa: E402


class setting:
    """Temporarily set LUCY_PROMPT_ORDER and fake the per-message memory/skills lookups."""
    def __init__(self, order=None, skills=False):
        self.order, self.skills = order, skills
    def __enter__(self):
        self.old = (os.environ.get("LUCY_PROMPT_ORDER"), api._memory_manager.get_contextual_memory, api._skills_manager.get_relevant_skills)
        if self.order is None: os.environ.pop("LUCY_PROMPT_ORDER", None)
        else: os.environ["LUCY_PROMPT_ORDER"] = self.order
        api._memory_manager.get_contextual_memory = lambda q: f"FACT-ABOUT<{q}>"
        api._skills_manager.get_relevant_skills = lambda q: ([{"name": "demo", "instructions": f"SKILL<{q}>"}] if self.skills else [])
        return self
    def __exit__(self, *a):
        old_order, api._memory_manager.get_contextual_memory, api._skills_manager.get_relevant_skills = self.old
        if old_order is None: os.environ.pop("LUCY_PROMPT_ORDER", None)
        else: os.environ["LUCY_PROMPT_ORDER"] = old_order


def common_prefix(a, b):
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n


def build(text):
    return api._build_messages("po-session", text)


def test_cache_prompt_is_requested_from_llama_server():
    assert api.llm.request_extras().get("cache_prompt") is True


def test_default_order_keeps_everything_that_never_changes_in_front_of_the_facts():
    with setting(None, skills=True):
        a, b = build("hello there"), build("something else entirely")
    n = common_prefix(a, b)
    assert api._prompt_order() == "static_first"
    assert n >= 3 and a[0]["content"] == api._persona_prompt            # persona + memory rules + tool rules (+ digest) are identical
    assert "FACT-ABOUT" in a[n]["content"] and "FACT-ABOUT" in b[n]["content"]      # the first difference IS the recalled facts
    assert all("FACT-ABOUT" not in m["content"] and "SKILL<" not in m["content"] for m in a[:n])
    assert [m["role"] for m in a[:n + 2]] == ["system"] * (n + 2)       # facts + skills are still system messages (no template risk)


def test_legacy_order_is_the_old_one_and_loses_the_cache_after_the_persona():
    with setting("legacy", skills=True):
        a, b = build("hello there"), build("something else entirely")
    assert common_prefix(a, b) == 1 and "FACT-ABOUT" in a[1]["content"]


def test_late_order_moves_facts_and_skills_onto_the_last_user_message():
    with setting("late", skills=True):
        a, b = build("hello there"), build("something else entirely")
    assert a[:-1] == b[:-1]                                              # everything before the user message is identical
    assert all("FACT-ABOUT" not in m["content"] for m in a[:-1] if isinstance(m["content"], str))
    last = a[-1]["content"]
    assert a[-1]["role"] == "user" and last.startswith("hello there") and "FACT-ABOUT<hello there>" in last and "SKILL<hello there>" in last


def test_an_unknown_order_falls_back_to_the_default():
    with setting("nonsense"):
        assert api._prompt_order() == "static_first"


# ---------------------------------------------------------------- the tool list is part of the prompt too
def _names(tools):
    return [t["function"]["name"] for t in tools]


def _turn(text, sid):
    return _names(api._tools_for_turn([{"role": "system", "content": "s"}, {"role": "user", "content": text}], None, sid)[0])


def test_tool_list_keeps_a_fixed_order_and_is_the_same_in_every_chat():
    a, b = _turn("what's the weather today?", "po-order-1"), _turn("what's the weather today?", "po-order-2")
    assert a == b and a[-1] == "tool_search"
    order = {n: i for i, n in enumerate(_names(api.TOOLS))}
    body = a[:-1]
    assert body == sorted(body, key=order.get)                           # same order as api.TOOLS, whatever the keyword order


def test_tools_picked_in_the_last_three_turns_stay_in_the_list():
    sid = "po-sticky-1"
    t1 = _turn("hi", sid); t2 = _turn("what's the weather today?", sid)
    assert "web_search" not in t1 and "web_search" in t2
    assert "web_search" in _turn("thanks", sid) and "web_search" in _turn("ok", sid)      # turns 3 and 4: still there
    assert "web_search" not in _turn("cool", sid)                                         # turn 5: the list shrinks back


def test_tools_the_model_pulled_in_mid_turn_are_kept_for_the_next_turns():
    sid = "po-added-1"
    assert "run_shell_command" not in _turn("hi", sid)
    api._note_added_tools(sid, ["run_shell_command"])                    # what AgentLoop reports after tool_search / auto-add
    assert "run_shell_command" in _turn("hi again", sid)
