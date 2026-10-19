"""P3 Office tools inside the agent loop (scripted fake llama-server): a delivered file counts as 'sent', problems stay visible
to the model, office_read calls run side by side. No GPU, no network."""
import asyncio, pathlib, sys
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_agent_loop import run, CALL, ANS, tool, S, tool_msgs  # noqa: E402
from lucy.server import agent_loop  # noqa: E402

TOOLS = [tool("docx_create", {"path": S, "markdown": S}, ["path"]), tool("office_read", {"path": S}, ["path"]),
         tool("send_file", {"file_path": S}, ["file_path"])]
ASK = [{"role": "user", "content": "Please make a word document report.docx and send it to me"}]


def test_a_file_the_office_tool_delivered_counts_as_sent_so_there_is_no_send_file_nudge():
    async def ex(name, args): return "MEDIA:report.docx"
    r = run([CALL("docx_create", {"path": "report.docx", "markdown": "# Hi"}), ANS("Here is your report.")], execute=ex, tools=TOOLS, messages=list(ASK))
    assert [c[0] for c in r.calls] == ["docx_create"] and r.text.endswith("Here is your report.")
    assert len(r.requests) == 2                                                          # no extra "you were asked to send the file" round
    assert [e.data for e in r.events if e.kind == "media"] == ["report.docx"]
    seen = tool_msgs(r)[0]["content"]
    assert seen.startswith("Delivered to the user's chat via docx_create") and "MEDIA:" not in seen.split("do not write")[0]


def test_a_problem_result_is_shown_to_the_model_in_full_and_nothing_is_delivered():
    msg = "Saved report.docx (3 blocks) but NOT sent to the chat yet, because: Slide 2 has too much text to fit."
    async def ex(name, args): return msg
    r = run([CALL("docx_create", {"path": "report.docx", "markdown": "# Hi"}), ANS("Fixing it.")], execute=ex, tools=TOOLS, messages=list(ASK))
    assert tool_msgs(r)[0]["content"] == msg and not [e for e in r.events if e.kind == "media"]


def test_office_read_calls_run_side_by_side():
    live, peak = [0], [0]
    async def slow(name, args):
        live[0] += 1; peak[0] = max(peak[0], live[0]); await asyncio.sleep(0.3); live[0] -= 1
        return "text of " + args["path"]
    call = lambda p: {"name": "office_read", "arguments": {"path": p}}
    r = run([{"tool_calls": [call("a.docx"), call("b.pdf")]}, ANS("done")], execute=slow, tools=TOOLS)
    assert peak[0] == 2 and [m["content"] for m in tool_msgs(r)] == ["text of a.docx", "text of b.pdf"]


def test_office_tools_are_known_to_the_loop():
    assert {"docx_create", "docx_edit", "xlsx_create", "xlsx_edit", "pptx_create", "pptx_edit", "pdf_create"} <= agent_loop.DELIVERS_FILES
    assert "office_read" in agent_loop.PARALLEL_TOOLS and "office_read" in agent_loop.READ_ONLY_TOOLS
    for name in ("docx_create", "docx_edit", "xlsx_create", "xlsx_edit", "pptx_create", "pptx_edit", "pdf_create", "office_read", "office_render"):
        assert name in agent_loop.AgentLoop._PLAYBOOK, name                              # every office tool has a "what to try when blocked" hint
