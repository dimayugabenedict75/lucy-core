import json, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src/lucy/server"))
from text_tool_calls import TextToolCallFilter

TOOLS = [
 {"function": {"name": "web_search", "parameters": {"type": "object", "required": ["query"],
    "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}}}},
 {"function": {"name": "edit_local_file", "parameters": {"type": "object", "required": ["file_path"],
    "properties": {"file_path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"}, "replace_all": {"type": "boolean"}}}}},
 {"function": {"name": "send_tts", "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}}},
]

def run(text, step=1):
    f = TextToolCallFilter(TOOLS); shown = ""
    for i in range(0, len(text), step): shown += f.feed(text[i:i+step])
    shown += f.finish()
    return f, shown

def args(c): return json.loads(c["arguments"])

def test_user_example_kwargs_and_no_leak():
    for step in (1, 3, 1000):
        f, shown = run('Sure, let me look that up.\nAction: web_search(query="latest GPU news", limit=3)', step)
        assert shown.rstrip() == "Sure, let me look that up.", repr(shown)
        c = f.calls(); assert len(c) == 1 and c[0]["name"] == "web_search"
        assert args(c[0]) == {"query": "latest GPU news", "limit": 3}

def test_json_arg_and_positional_and_bare():
    f, _ = run('Action: web_search({"query": "cats", "limit": 2})'); assert args(f.calls()[0]) == {"query": "cats", "limit": 2}
    f, _ = run('Action: web_search("dog food")'); assert args(f.calls()[0]) == {"query": "dog food"}
    f, _ = run('Action: web_search(query: "x y", limit: 4)'); assert args(f.calls()[0]) == {"query": "x y", "limit": 4}

def test_react_two_line():
    f, shown = run('Action: web_search\nAction Input: {"query": "rtx 5090"}')
    assert shown == "" and args(f.calls()[0]) == {"query": "rtx 5090"}

def test_edit_local_file_multiline_triple_quotes():
    t = 'Action: edit_local_file(file_path="C:/x/a.py", old_string="""def f():\n    return (1)\n""", new_string="def f(): return 2", replace_all=False)'
    f, _ = run(t, 5); a = args(f.calls()[0])
    assert a["old_string"] == "def f():\n    return (1)\n" and a["replace_all"] is False and a["file_path"] == "C:/x/a.py"

def test_tool_call_xml_mistral_and_function_xml():
    f, _ = run('<tool_call>{"name": "web_search", "arguments": {"query": "a"}}</tool_call>'); assert args(f.calls()[0]) == {"query": "a"}
    f, _ = run('[TOOL_CALLS][{"name": "web_search", "arguments": {"query": "b"}}]'); assert args(f.calls()[0]) == {"query": "b"}
    f, _ = run('<function=web_search><parameter=query>c</parameter><parameter=limit>7</parameter></function>')
    assert args(f.calls()[0]) == {"query": "c", "limit": 7}

def test_multiple_calls_and_dedupe():
    f, _ = run('Action: web_search(query="a")\nAction: send_tts(text="hi")\nAction: web_search(query="a")')
    assert [c["name"] for c in f.calls()] == ["web_search", "send_tts"]

def test_normal_text_is_untouched():
    for t in ("The Action: items list is below.", "Take action: now!", "Use <b>bold</b> and [1] refs, a < b.",
              "Action: unknown_tool(x=1)", "I will use web_search later."):
        f, shown = run(t, 2); assert shown == t and not f.tripped and f.calls() == [], t

def test_truncated_call_is_not_run_and_text_is_recoverable():
    f, shown = run('Okay.\nAction: web_search(query="cut off here')
    assert f.tripped and f.calls() == [] and f.held_text.startswith("Action: web_search")
    assert shown == "Okay.\n"

def test_unknown_tool_ignored():
    f, _ = run('<tool_call>{"name": "rm_rf", "arguments": {}}</tool_call>'); assert f.calls() == []
