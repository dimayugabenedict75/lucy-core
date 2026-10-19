"""Reliable agentic loop for Lucy Core.

`AgentLoop.run(messages)` talks to llama-server, executes the tools the model asks for, feeds the
results back, and repeats until the model gives a final answer. It yields small `Event`s
(content / activity / media / error / metrics) that api.py turns into the SSE stream, so the loop
itself knows nothing about HTTP routes, TTS or sessions and can be tested on its own.

What it guards against (each one was a real way for the old loop to fail silently or crash):

  MODEL
    * llama-server down / still loading / 503      -> waits (and starts the model) instead of an error
    * transient network errors before any output   -> retried
    * "exceeds context" (HTTP 400)                 -> old tool output is pruned, then retried
    * context creeping up during a long turn       -> pruned *before* the request
  TOOL CALLS
    * tool call with finish_reason "stop"         -> still executed (it used to be dropped)
    * empty / duplicate call ids, empty names     -> repaired
    * invalid JSON arguments                      -> NOT executed (used to run with {}); the model is
                                                     told exactly what was wrong; history is kept valid
    * unknown tool / missing required arguments   -> clear error back to the model, nothing runs
    * "5" instead of 5, "true" instead of true    -> coerced from the tool's schema
    * output cut off mid tool call (length)       -> not executed; told to use smaller pieces
  EXECUTION
    * a tool raising or hanging                   -> becomes an error result the model can react to
    * huge results                                -> capped (head + tail) so they can't flood the context
    * identical read-only call repeated           -> cached result reused; forced to answer if it persists
    * the same tool failing again and again       -> told to change approach; turn ends if it keeps going
  REPLIES
    * empty reply (esp. right after tools)        -> nudged to answer; plain fallback if it still is empty
    * reply cut off by the token limit            -> auto-continued
    * "Let me search for that." and then stopping -> escalation ladder (multistep.py): nudge, then the
                                                     harness runs the announced search itself, then an
                                                     honest "I didn't do it" instead of silence
    * multi-step task with steps still pending    -> `update_plan` checklist; the turn can't end on a plain
                                                     reply until every step is done or skipped
    * rounds exhausted                            -> one last tool-less round that must report honestly
"""

from __future__ import annotations

import ast
import asyncio
import difflib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx

from lucy.server.multistep import (FINISH_TOOL, FINISH_TOOL_NAME, PLAN_TOOL, PLAN_TOOL_NAME, Plan, Promise,
                                   expected_actions, find_promise)
from lucy.server.repeat_guard import RepeatGuard
from lucy.server.text_tool_calls import TextToolCallFilter
from lucy.tools.routing import TOOL_SEARCH_NAME, describe_tools

logger = logging.getLogger("lucy.core.agent")
VERIFY_NAMES = frozenset({"edit_local_file", "write_local_file", "apply_patch"})

# Tools that only read: an identical repeat within the same state of the world is wasted work.
READ_ONLY_TOOLS = frozenset({
    "web_search", "web_extract", "read_local_file", "list_local_files", "find_file", "recall_memory",
    "search_conversations", "get_doc_content", "list_drive_files", "analyze_image", "code_search", "project_map", "office_read",
})

# Independent reads that can safely run side by side in one round (no shared client/state, no GPU slot, no approval of
# their own). Google tools share one non-thread-safe client and analyze_image needs the single llama slot: not listed.
PARALLEL_TOOLS = frozenset({"read_local_file", "list_local_files", "find_file", "code_search", "project_map",
                            "web_search", "web_extract", "recall_memory", "search_conversations", "office_read"})

# Tools that put the file they made into the chat themselves (MEDIA: result): that counts as "sent the file".
DELIVERS_FILES = frozenset({"docx_create", "docx_edit", "xlsx_create", "xlsx_edit", "pptx_create", "pptx_edit", "pdf_create"})

_NOTE = "[Harness note - internal, do not mention or quote it] "

# "I saved it to memory" / "I updated api.py" / "I sent the sticker" / "I searched the web" - said without the
# tool call that would make it true. (verbs, objects, tools that would back the claim)
_CLAIM_FAMILIES = [
    ("saved something to memory", r"saved|stored|added|noted|recorded|committed|written|logged|remembered",
     r"memory|persistent|long[- ]term|remember", {"remember"}),
    ("changed a file", r"updated|edited|patched|modified|changed|rewrote|written|wrote|created|saved|fixed|appended|added",
     r"file|\.py\b|\.md\b|\.json\b|api\.py|lucy\.md|\bcode\b|persona|harness|script",
     {"edit_local_file", "write_local_file", "apply_patch", "format_and_lint", "docx_create", "docx_edit", "xlsx_create", "xlsx_edit",
      "pptx_create", "pptx_edit", "pdf_create"}),
    ("ran the tests", r"ran|run|tested|verified|executed|checked", r"\btests?\b|test suite|pytest|unit tests",
     {"run_tests", "run_shell_command", "python"}),
    ("sent something to the chat", r"sent|attached|shared|dropped|posted|delivered",
     r"sticker|image|picture|file|screenshot", {"send_sticker", "send_file"}),
    ("searched the web", r"searched|looked\s+up|googled|browsed",
     r"web|online|internet|google|results", {"web_search", "web_extract"}),
    ("set or changed a reminder", r"set|scheduled|created|added|saved|updated|changed|cancell?ed|removed|deleted",
     r"reminder|alarm|timer|follow-?up", {"set_reminder", "schedule_task", "manage_reminders"}),
]
_CLAIM_SKIP = re.compile(r"\b(?:earlier|previously|last\s+time|before|yesterday|already|if|once|after|when|unless|whether|"
                         r"would|could|should|will|can|want|need)\b", re.IGNORECASE)
_CLAIM_SUBJECT = r"(?:\bI(?:['’]ve|\s+have|\s+just|\s+also|\s+went\s+ahead\s+and|\s+successfully)?\s+(?:\w+\s+){0,2}?|" \
                 r"\b(?:has|have)\s+been\s+|\bsuccessfully\s+|^\W*)"


def unbacked_claim(text: str, ok_tools: set) -> tuple[str, set] | None:
    """If `text` says an action was done that no successful tool call this turn backs up -> (sentence, tools)."""
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        s = sentence.strip()
        if not s or s.endswith("?") or _CLAIM_SKIP.search(s):
            continue
        for _label, verbs, objects, tools in _CLAIM_FAMILIES:
            if re.search(_CLAIM_SUBJECT + r"(?:" + verbs + r")\b", s, re.IGNORECASE) \
                    and re.search(objects, s, re.IGNORECASE) and not (tools & ok_tools):
                return s, tools
    return None


_LOCAL_HINT = re.compile(r"find_file|list_local_files|read_local_file|edit_local_file|write_local_file|"
                         r"run_shell_command|\b(?:director(?:y|ies)|folders?|filesystem|file\s?names?|paths?)\b|"
                         r"\b[\w\-]+\.(?:py|js|ts|md|txt|json|ya?ml|html|css|docx?|xlsx?|pdf|csv|zip|png|jpg)\b|"
                         r"[A-Za-z]:[\\/]|/Users/|/home/", re.IGNORECASE)

_FAIL_PREFIXES = ("error", "file not found", "directory not found", "image not found", "tool failed",
                  "tool timed out", "unknown tool", "invalid arguments", "missing required", "refused",
                  "no change", "not a file", "[error", "permission denied", "access denied", "not allowed",
                  "command failed", "traceback (most recent call last)")


@dataclass
class Event:
    kind: str                   # content | activity | media | error | metrics
    data: Any = None


@dataclass
class LoopConfig:
    max_rounds: int = 0             # SAFETY CEILING on tool rounds per turn; 0 = none. A task is never cut short by a
                                    # round budget: it ends when it is done, when you stop it, or when it is stuck (max_stall)
    max_tokens: int = 4096          # per model call
    tool_timeout: float = 120.0     # seconds per tool call
    max_result_chars: int = 12000   # cap on one tool result fed back to the model
    ctx_tokens: int = 100000        # llama-server context size
    ctx_safety: float = 0.75        # token counts are estimates; leave headroom
    http_timeout: float = 600.0
    connect_timeout: float = 5.0
    ready_wait_s: float = 300.0     # how long to wait for a loading / crashed model
    http_retries: int = 2
    max_empty_retries: int = 4
    max_continuations: int = 6
    max_repeat_calls: int = 3       # identical read-only repeats before the "use what you have" push gets firm
    max_tool_errors: int = 8        # (kept for compatibility; failures never end a turn - they trigger workarounds)
    narration_guard: bool = True
    max_promise_nudges: int = 1     # after this many nudges the harness makes the call itself (constrained JSON request)
    max_auto_acts: int = 2          # times per turn the harness may run an announced search itself
    plan_tool: bool = True          # offer the update_plan checklist tool
    max_plan_nudges: int = 2        # after this many reminders the next round REQUIRES a tool call
    max_stall: int = 30             # consecutive rounds with no progress (only failures / repeats / nudges) -> honest report
    max_repeat_trips: int = 4       # times the stream may be cut for repeating itself before the turn reports and ends
    max_cover_nudges: int = 2       # "you were asked to send/edit a file but no tool did" reminders per turn
    max_idle: int = 8               # consecutive rounds with NO tool call (nudges that were ignored, even when a tool
                                    # call was required) before the turn reports honestly instead of nudging forever


@dataclass
class RoundResult:
    content: str = ""               # everything the model said (visible text)
    tool_calls: list = field(default_factory=list)      # [{"id","name","arguments"(str)}]
    finish_reason: str | None = None
    emitted: bool = False
    degenerate: bool = False        # the model started repeating itself and the stream was cut
    raw: str = ""                   # what the model wrote, before repeated sentences were removed (for judging it)
    held: str = ""                  # visible text withheld while the turn is in "push" mode (shown only if it is the answer)


@dataclass
class TurnState:
    emitted_any: bool = False
    last_char: str = ""
    rounds: int = 0
    tool_calls_total: int = 0
    tool_errors: int = 0
    nudges: int = 0
    empty_retries: int = 0
    continues: int = 0
    retries: int = 0
    repeats: int = 0
    truncated_calls: int = 0
    epoch: int = 0                  # bumps whenever a non-read-only tool succeeds
    cache: dict = field(default_factory=dict)
    fail_streak: dict = field(default_factory=dict)
    context_retry: bool = False
    force_final: bool = False
    last_result: str = ""
    glue: bool = False              # next text continues a cut-off reply: no paragraph break
    ok_tools: set = field(default_factory=set)   # names of tools that succeeded this turn
    claim_nudges: int = 0
    promise_streak: int = 0         # consecutive replies that announced an action without taking it
    auto_acts: int = 0              # searches the harness ran on the model's behalf
    auto_calls: list = field(default_factory=list)
    plan: Plan = field(default_factory=Plan)
    search_updates: int = 0         # tool_search calls this turn (bookkeeping, capped like plan updates)
    # --- persistence: keep going until done, find a way round blockers ---
    stall: int = 0                  # consecutive rounds with no real progress
    idle: int = 0                   # consecutive rounds without any tool call (ignored nudges)
    progress: bool = False          # set by a tool call that succeeded with something new this round
    force_tool: bool = False        # next round is a harness-made tool call (grammar-constrained, see _forced_round)
    forced_broken: bool = False     # the server can't do constrained output: fall back to tool_choice=required / nudges
    release_held: bool = False      # the held text is real (e.g. a reply cut by the length limit): show it
    required_broken: bool = False   # the server rejected tool_choice=required: use nudges only
    anti_repeat: bool = False       # sampling penalties on (after the model repeated itself)
    repeat_trips: int = 0
    cover_nudges: int = 0
    wanted: list = field(default_factory=list)  # [(label, tools)] the user explicitly asked for
    sig_failed: dict = field(default_factory=dict)   # (tool, args) -> (times failed, epoch, message)
    guard: RepeatGuard = field(default_factory=RepeatGuard)


class _Retry(Exception):
    def __init__(self, kind: str, detail: str = "", status: int | None = None):
        super().__init__(detail)
        self.kind, self.detail, self.status = kind, detail, status


# ---------------------------------------------------------------------------------------------
def parse_args(raw: Any) -> tuple[dict | None, str | None]:
    """Tool arguments -> (dict, None) or (None, reason). Tolerates fences and Python-literal dicts."""
    if isinstance(raw, dict):
        return raw, None
    s = (raw or "").strip() if isinstance(raw, str) else ""
    if not s:
        return {}, None
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", s, re.DOTALL)
    if m:
        s = m.group(1)
    for loader in (lambda: json.loads(s, strict=False), lambda: ast.literal_eval(s)):
        try:
            v = loader()
        except Exception:
            continue
        return (v, None) if isinstance(v, dict) else (None, "arguments must be a JSON object")
    return None, f"arguments are not valid JSON (they start with: {s[:80]!r})"


def coerce_args(args: dict, schema: dict) -> dict:
    """Fix the usual small-model slips using the tool's JSON schema ("3" -> 3, "true" -> True ...)."""
    props = (schema or {}).get("properties") or {}
    out = dict(args)
    for key, spec in props.items():
        if key not in out or out[key] is None:
            continue
        v, typ = out[key], (spec or {}).get("type")
        try:
            if typ == "integer" and isinstance(v, (str, float)) and not isinstance(v, bool):
                out[key] = int(float(v))
            elif typ == "number" and isinstance(v, str):
                out[key] = float(v)
            elif typ == "boolean" and isinstance(v, str):
                out[key] = v.strip().lower() in ("true", "1", "yes", "y")
            elif typ in ("array", "object") and isinstance(v, str) and v.strip()[:1] in ("[", "{"):
                out[key] = json.loads(v)
            elif typ == "array" and isinstance(v, str):
                out[key] = [v]
            elif typ == "string" and isinstance(v, (int, float)) and not isinstance(v, bool):
                out[key] = str(v)
        except (ValueError, TypeError):
            pass
    return out


def looks_failed(result: str) -> bool:
    head = result.lstrip()[:200].lower()
    if head.startswith(_FAIL_PREFIXES):
        return True
    if head.startswith("{") and '"error"' in head and '"status": "ok"' not in head:
        return True
    return False


def narrates_without_acting(text: str) -> bool:
    """True if a (short) reply ends by announcing an action it never took (emoji-proof, see multistep)."""
    return find_promise(text, max_chars=700) is not None


# ---------------------------------------------------------------------------------------------
class AgentLoop:
    """
    brain          object with: base_url, model, label (str props); extras() -> dict; loading() -> bool;
                   error() -> str|None; async ready() -> bool; async ensure() -> None
    tools          OpenAI-format tool definitions offered to the model
    execute        async (name, args) -> str
    activity_for   (name, args) -> activity label for the UI
    count_tokens   (text) -> int
    trace          optional callable(dict) for per-call / per-turn logging
    gate           optional async (name, args, emit) -> None | refusal text (the P1 permission layer)
    """

    def __init__(self, *, brain, tools: list[dict], execute: Callable[[str, dict], Awaitable[str]],
                 activity_for: Callable[[str, dict], str], count_tokens: Callable[[str], int],
                 temperature: float, cfg: LoopConfig | None = None,
                 trace: Callable[[dict], None] | None = None, session_id: str | None = None,
                 tool_catalog: Any = None, on_tools_added: Callable[[list], None] | None = None,
                 gate: Callable[[str, dict, Callable[[str, Any], None] | None], Awaitable[str | None]] | None = None,
                 verify: Any = "default"):
        self.cfg = cfg or LoopConfig()
        # VERIFY step (lucy.tools._verify): after a successful file write/edit the harness checks syntax (and, if the user
        # enabled LUCY_VERIFY=tests, runs the tests) and appends the outcome to the tool result. None = off.
        if verify == "default":
            from lucy.tools import _verify
            verify = _verify.verify_after
        self.verify = verify
        self._gate_lock = asyncio.Lock()                    # approval cards appear one at a time, even for parallel calls
        # Permission gate (lucy.safety): awaited right before a tool runs. None = run it; a string = not allowed (it
        # becomes the tool result). It may pause the turn to ask the user: it reports that through `emit`
        # (approval_request / approval_resolved / activity events), which the loop streams out while it waits.
        self.gate = gate
        self._emit: Callable[[str, Any], None] | None = None
        self.tool_catalog = tool_catalog          # optional ToolCatalog: tool_search + auto-adding tools not shown this turn
        self.on_tools_added = on_tools_added      # called with the new tool names (api.py remembers them for the next turns)
        if self.cfg.plan_tool and not any(t.get("function", {}).get("name") == PLAN_TOOL_NAME for t in tools):
            tools = list(tools) + [PLAN_TOOL]
        self.brain, self.tools, self.execute = brain, list(tools), execute      # own copy: tools can be added mid-turn
        self.activity_for, self.count, self.temperature = activity_for, count_tokens, temperature
        self._trace_cb, self.session_id = trace, session_id
        self.schemas = {t["function"]["name"]: t["function"].get("parameters", {}) or {}
                        for t in tools if t.get("function", {}).get("name")}
        try:
            self._tools_tokens = self.count(json.dumps(tools))
        except Exception:
            self._tools_tokens = 4000

    def _add_tools(self, specs) -> list[str]:
        """Make more tools available from now on (tool_search / auto-add). Returns the names that were new."""
        added = []
        for t in specs:
            fn = t.get("function") or {}
            name = fn.get("name")
            if not name or name in self.schemas:
                continue
            self.tools.append(t)
            self.schemas[name] = fn.get("parameters", {}) or {}
            added.append(name)
        if added:
            try:
                self._tools_tokens = self.count(json.dumps(self.tools))
            except Exception:
                pass
            if self.on_tools_added:
                try:
                    self.on_tools_added(list(added))
                except Exception:
                    pass
        return added

    def _trace(self, **kw) -> None:
        if self._trace_cb:
            try:
                self._trace_cb({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "session": self.session_id, **kw})
            except Exception:
                pass

    # =========================================================================== main loop
    @staticmethod
    def _last_user_text(messages: list[dict]) -> str:
        """The user's latest real message (not a harness note)."""
        for m in reversed(messages):
            if m.get("role") != "user":
                continue
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
            if isinstance(c, str) and c.strip() and not c.startswith(_NOTE):
                return c
        return ""

    async def run(self, messages: list[dict]) -> AsyncIterator[Event]:
        """Work on the user's request until it is DONE.

        The turn does not end because a step budget ran out or because a tool failed. It ends when the model
        gives a real final answer (or calls finish_task), when the user stops it, or - as a last resort - when
        `max_stall` rounds in a row made no progress at all (then it reports honestly what it tried)."""
        cfg, st = self.cfg, TurnState()
        t0 = time.monotonic()
        finish = "ok"
        final_rounds = 0
        st.wanted = expected_actions(self._last_user_text(messages))
        yield Event("activity", "think")
        while True:
            st.rounds += 1
            ceiling = bool(cfg.max_rounds) and st.rounds > cfg.max_rounds
            stalled = st.stall >= cfg.max_stall or st.idle >= cfg.max_idle
            if (ceiling or stalled) and not st.force_final:
                st.force_final = True
                finish = "stalled" if stalled else "ceiling"
                why = ("You have been going around in circles: several rounds in a row made no progress. "
                       if stalled else f"This turn reached its safety limit of {cfg.max_rounds} tool rounds. ")
                messages.append({"role": "user", "content": _NOTE + why + (
                    "You cannot call any more tools this turn. Do not present guesses as facts. Tell the user "
                    "plainly: what is DONE (confirmed by tool results), what is NOT done, what blocked you and "
                    "what you already tried, and the one concrete thing that would unblock it. They can say "
                    "'continue' and you will pick up from there.")})
            final_round = st.force_final
            if final_round:
                final_rounds += 1
                if final_rounds > 8:                       # the closing report itself kept failing
                    break
            self._prune(messages)
            # Every model call is announced. Retries (nudges, auto-continue, empty-reply retries) used to run
            # silently, and the UI hides its indicator once text streams in - so those rounds looked frozen.
            yield Event("activity", "think")

            if final_round:
                round_tools = []
            elif st.force_tool and st.forced_broken and not st.required_broken:
                round_tools = list(self.tools) + [FINISH_TOOL]
            else:
                round_tools = self.tools
            res: RoundResult | None = None
            if st.force_tool and not st.forced_broken and not final_round and self.tools:
                # The model keeps announcing an action instead of calling the tool. Don't ask again: have the server
                # produce the call itself (a grammar-constrained request that can only answer with a tool call).
                res = await self._forced_round(messages, st)
                if res is None:
                    st.forced_broken = True
                    self._trace(event="forced_unavailable", round=st.rounds)
                    if not st.required_broken:             # next best: tool_choice=required, with finish_task as the exit
                        round_tools = list(self.tools) + [FINISH_TOOL]
            if res is None:
                async for ev in self._round(messages, round_tools, st):
                    if ev.kind == "_round":
                        res = ev.data
                    else:
                        yield ev
                st.glue = False
                if res is None:                          # fatal error already reported
                    finish = "error"
                    break
            st.glue = False

            calls = [] if final_round else self._normalize_calls(res, st)

            # --- the model got stuck repeating itself: the stream was cut; restart it on a different footing ---
            if res.degenerate and not (calls and all(parse_args(c["arguments"])[0] is not None for c in calls)):
                st.repeat_trips += 1
                st.anti_repeat = True
                st.stall += 1
                st.idle += 1
                self._trace(event="repeat_cut", round=st.rounds, trips=st.repeat_trips, dropped=st.guard.dropped)
                if st.repeat_trips > cfg.max_repeat_trips:
                    msg = ("I kept getting stuck repeating myself, so I stopped that train of thought. "
                           "Tell me to continue and I'll pick the task up from where it stands.")
                    yield Event("content", self._sep(st, msg))
                    st.emitted_any, st.last_char = True, msg[-1:]
                    finish = "repeat"
                    break
                if res.content.strip():
                    messages.append({"role": "assistant", "content": res.content})
                messages.append({"role": "user", "content": _NOTE + (
                    "Give the final answer now, in two or three sentences, without restating earlier points."
                    if final_round else
                    "Carry on with the next concrete step of the task by calling the right tool. Write nothing "
                    "that was already said. If the whole task is finished, give a short final answer.")})
                if st.repeat_trips >= 2:
                    st.force_tool = True
                continue

            if calls:
                fin = [c for c in calls if c["name"] == FINISH_TOOL_NAME]
                if fin:
                    others = [c for c in calls if c["name"] != FINISH_TOOL_NAME]
                    pending = st.plan.pending()
                    if others:
                        calls = others                   # real work first; finish_task is ignored this round
                    elif pending:
                        self._reject_finish(messages, res, fin, st)
                        st.stall += 1
                        continue
                    else:
                        args, _err = parse_args(fin[0]["arguments"])
                        summary = str((args or {}).get("summary") or "").strip()
                        if summary:
                            yield Event("content", self._sep(st, summary))
                            st.emitted_any, st.last_char = True, summary[-1:]
                        elif not st.emitted_any:
                            yield Event("content", "Done.")
                        break
                if res.finish_reason == "length" and any(parse_args(c["arguments"])[0] is None for c in calls):
                    st.truncated_calls += 1
                    st.stall += 1
                    self._trace(event="truncated_tool_call", round=st.rounds)
                    if res.content.strip():
                        messages.append({"role": "assistant", "content": res.content})
                    messages.append({"role": "user", "content": _NOTE + (
                        "Your tool call was cut off by the output limit, so it was NOT run. Redo it with a much "
                        "smaller payload. For a big file: create it small with write_local_file, then add the rest "
                        "in several edit_local_file steps (or append pieces with run_shell_command)."
                        + (" Do not retry the same large call again." if st.truncated_calls > 1 else ""))})
                    continue
                st.progress = False
                st.idle = 0
                if res.held.strip() and not find_promise(res.held):
                    held_out = self._sep(st, res.held)
                    st.emitted_any, st.last_char = True, held_out[-1:]
                    yield Event("content", held_out)
                async for ev in self._run_tools(res, calls, messages, st):
                    yield ev
                st.promise_streak = 0                    # it acted: the last promise (if any) was kept
                bookkeeping = all(c["name"] in (PLAN_TOOL_NAME, TOOL_SEARCH_NAME) for c in calls)
                if bookkeeping and st.plan.updates <= 4 and st.search_updates <= 4:
                    st.rounds -= 1                       # bookkeeping isn't work: don't spend a round on it
                elif st.progress:
                    st.stall = 0
                    st.force_tool = False
                else:
                    st.stall += 1                        # failures / repeats only: the workaround hints are in the results
                yield Event("activity", None)
                yield Event("activity", "think")
                continue

            st.release_held = False
            verdict, extra = self._judge_final(res, messages, st, final_round)
            if verdict == "retry":
                if st.release_held and res.held.strip():       # e.g. a real answer cut by the length limit
                    held_out = self._sep(st, res.held)
                    st.emitted_any, st.last_char = True, held_out[-1:]
                    yield Event("content", held_out)
                st.stall += 1
                st.idle += 1
                continue
            if verdict == "auto":                        # the model keeps announcing a search: run it for it
                auto, st.auto_calls = st.auto_calls, []
                st.auto_acts += 1
                self._trace(event="auto_act", round=st.rounds, calls=[(c["name"], c["arguments"][:120]) for c in auto])
                st.progress = False
                async for ev in self._run_tools(res, auto, messages, st):
                    yield ev
                st.stall = 0 if st.progress else st.stall + 1
                yield Event("activity", None)
                yield Event("activity", "think")
                continue
            if verdict == "done" and res.held.strip():         # it was held back, and it is the final answer
                held_out = self._sep(st, res.held)
                st.emitted_any, st.last_char = True, held_out[-1:]
                yield Event("content", held_out)
            if verdict == "fallback":
                yield Event("content", self._sep(st, extra))
                st.emitted_any, st.last_char = True, extra[-1:]
            break

        self._trace(event="turn", rounds=st.rounds, tool_calls=st.tool_calls_total, tool_errors=st.tool_errors,
                    nudges=st.nudges, auto_acts=st.auto_acts, plan_steps=len(st.plan.steps), empty_retries=st.empty_retries,
                    continues=st.continues, retries=st.retries, repeats=st.repeats, stall=st.stall,
                    repeat_cuts=st.repeat_trips, repeats_dropped=st.guard.dropped, finish=finish,
                    ms=int((time.monotonic() - t0) * 1000))
        yield Event("metrics", {"rounds": st.rounds, "tool_calls": st.tool_calls_total,
                                "tool_errors": st.tool_errors, "retries": st.retries})

    def _reject_finish(self, messages: list[dict], res: RoundResult, fin: list[dict], st: TurnState) -> None:
        """finish_task while plan steps are still open: answer the call (keeps the history valid) and push on."""
        messages.append({"role": "assistant", "content": res.content, "tool_calls": [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": "{}"}} for c in fin]})
        for c in fin:
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": (
                "Not finished: your plan still has open steps:\n" + st.plan.render() + "\n" + _NOTE +
                f"Do the next one now: {st.plan.next_step()}. If a step is no longer needed, mark it skipped "
                "with update_plan.")})

    # =========================================================================== a harness-made tool call
    @staticmethod
    def _slim_schema(s):
        """A JSON-schema fragment cut down to what llama.cpp's schema->grammar converter reliably supports."""
        if not isinstance(s, dict):
            return {}
        out = {k: s[k] for k in ("type", "enum", "required") if k in s}
        if "properties" in s and isinstance(s["properties"], dict):
            out["properties"] = {k: AgentLoop._slim_schema(v) for k, v in s["properties"].items()}
            out["additionalProperties"] = False
        if "items" in s:
            out["items"] = AgentLoop._slim_schema(s["items"])
        if "type" not in out and "enum" not in out and "properties" not in out:
            out["type"] = "string"
        return out

    async def _forced_round(self, messages: list[dict], st: TurnState) -> RoundResult | None:
        """Get a tool call out of the model NOW, whatever its chat template or the server's tool_choice support.

        A separate, non-streaming request whose output is constrained by a JSON schema to be exactly one call:
        {"tool": <one of the tools>, "arguments": {...valid for that tool...}} (or finish_task). The model still
        decides WHAT to call - it just can't answer with prose. Returns None when the server can't do this."""
        cfg, brain = self.cfg, self.brain
        alts = []
        for t in list(self.tools) + [FINISH_TOOL]:
            fn = t["function"]
            params = self._slim_schema(fn.get("parameters") or {"type": "object", "properties": {}})
            params.setdefault("type", "object")
            alts.append({"type": "object", "additionalProperties": False, "required": ["tool", "arguments"],
                         "properties": {"tool": {"enum": [fn["name"]]}, "arguments": params}})
        schema = {"anyOf": alts}
        ask = {"role": "user", "content": _NOTE + (
            'Reply with ONLY a JSON object that is the single tool call to make right now: '
            '{"tool": "<tool name>", "arguments": {...}}. Pick the tool that does the next step of what the user '
            'asked (if a tool failed earlier, use a different approach). Use "finish_task" with a "summary" only '
            "if the user's whole request is already completely done.")}
        base = {"model": brain.model, "messages": list(messages) + [ask], "stream": False,
                "temperature": 0.2, "max_tokens": min(cfg.max_tokens, 4096), **brain.extras()}
        for k in ("repeat_penalty", "frequency_penalty", "presence_penalty", "dry_multiplier"):
            base.pop(k, None)                       # sampling penalties would damage JSON
        variants = [{"response_format": {"type": "json_schema", "json_schema": {"name": "tool_call", "schema": schema}}},
                    {"json_schema": schema}]
        try:
            timeout = httpx.Timeout(cfg.http_timeout, connect=cfg.connect_timeout)
            async with httpx.AsyncClient(timeout=timeout) as client:
                for extra in variants:
                    resp = await client.post(f"{brain.base_url}/chat/completions", json={**base, **extra})
                    if resp.status_code != 200:
                        continue
                    text = ((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
                    try:
                        obj = json.loads(text)
                    except ValueError:
                        continue
                    name, args = obj.get("tool"), obj.get("arguments")
                    if name not in {t["function"]["name"] for t in self.tools} | {FINISH_TOOL_NAME} \
                            or not isinstance(args, dict):
                        continue
                    self._trace(event="forced_call", round=st.rounds, tool=name,
                                args=json.dumps(args, ensure_ascii=False)[:200])
                    res = RoundResult(finish_reason="tool_calls")
                    res.tool_calls = [{"id": f"forced_{st.rounds}", "name": name,
                                       "arguments": json.dumps(args, ensure_ascii=False)}]
                    return res
        except asyncio.CancelledError:
            raise
        except Exception as e:                                  # noqa: BLE001 - fall back to the normal path
            logger.warning(f"forced tool call failed: {e}")
        return None

    # =========================================================================== one model call
    def _sep(self, st: TurnState, text: str) -> str:
        """Keep text from different rounds apart ("...that.Here" -> "...that.\\n\\nHere")."""
        if st.glue:
            return text
        if text and st.emitted_any and st.last_char and not st.last_char.isspace() and not text[0].isspace():
            return "\n\n" + text
        return text

    def _note_emitted(self, st: TurnState, text: str) -> None:
        if text:
            st.emitted_any, st.last_char = True, text[-1]

    async def _round(self, messages: list[dict], tools: list[dict], st: TurnState) -> AsyncIterator[Event]:
        """One LLM request with retry / wait-for-model handling. Ends with Event("_round", RoundResult|None)."""
        cfg, brain = self.cfg, self.brain
        attempt, waited_since, down_tries = 0, None, 0
        while True:
            attempt += 1
            res, tcf = RoundResult(), TextToolCallFilter(tools)
            first_visible, announced_call = True, False
            st.guard.begin_round()
            dropped_before = st.guard.dropped
            # While the harness is pushing the model to act (it announced something and stopped, left plan steps open,
            # repeated itself...), its text is narration of the same intention. Hold it back: it is shown only if it
            # turns out to be the actual answer. Otherwise the chat fills with "I'm going to..." paragraphs.
            hold = bool(tools) and (st.promise_streak > 0 or st.force_tool or st.plan.nudges > 0 or st.cover_nudges > 0
                                    or st.claim_nudges > 0 or st.repeat_trips > 0)
            payload = {"model": brain.model, "messages": messages, "temperature": self.temperature,
                       "max_tokens": cfg.max_tokens, "tools": tools, "stream": True,
                       "stream_options": {"include_usage": True}, **brain.extras()}
            if tools:
                # After the model has twice announced an action without doing it, the round MUST contain a tool call
                # (finish_task is the way out when it really is finished).
                payload["tool_choice"] = "required" if (st.force_tool and not st.required_broken) else "auto"
            if st.anti_repeat:                  # it repeated itself: discourage that (llama.cpp ignores unknown keys)
                payload["repeat_penalty"] = max(float(payload.get("repeat_penalty", 1.0)), 1.1)
                payload["frequency_penalty"] = max(float(payload.get("frequency_penalty", 0.0)), 0.2)
                payload["dry_multiplier"] = max(float(payload.get("dry_multiplier", 0.0)), 0.5)
                payload["dry_base"], payload["dry_allowed_length"] = 1.75, 4     # long runs only: JSON stays intact
            failure: _Retry | None = None
            try:
                timeout = httpx.Timeout(cfg.http_timeout, connect=cfg.connect_timeout)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream("POST", f"{brain.base_url}/chat/completions", json=payload) as resp:
                        if resp.status_code != 200:
                            body = (await resp.aread()).decode("utf-8", "replace")[:600]
                            raise _Retry(self._classify(resp.status_code, body), body, resp.status_code)
                        tcs: list[dict] = []
                        async for line in resp.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            data = line[6:]
                            if data == "[DONE]":
                                break
                            try:
                                choice = (json.loads(data).get("choices") or [None])[0]
                            except (json.JSONDecodeError, AttributeError):
                                continue
                            if not choice:
                                continue
                            delta = choice.get("delta") or {}
                            piece = delta.get("content") or ""
                            if piece:
                                res.content += piece
                                piece = tcf.feed(piece)
                                if piece:
                                    piece = st.guard.feed(piece)      # drops sentences it already said
                                    if piece and hold:
                                        res.held += piece
                                        res.emitted = True
                                    elif piece:
                                        out = self._sep(st, piece) if first_visible else piece
                                        first_visible = False
                                        res.emitted = True
                                        self._note_emitted(st, out)
                                        yield Event("content", out)
                                    if st.guard.tripped:               # stuck in a loop: stop the generation now
                                        res.degenerate, res.finish_reason = True, "repeat"
                                        break
                            for tc in delta.get("tool_calls") or []:
                                if not announced_call:      # the model is now writing a tool call (can take a while,
                                    announced_call = True   # and no text is streaming): keep the UI "busy"
                                    yield Event("activity", "think")
                                i = tc.get("index", 0)
                                while len(tcs) <= i:
                                    tcs.append({"id": "", "name": "", "arguments": ""})
                                fn = tc.get("function") or {}
                                if tc.get("id"):
                                    tcs[i]["id"] = tc["id"]
                                if fn.get("name"):
                                    tcs[i]["name"] += fn["name"] if tcs[i]["name"] != fn["name"] else ""
                                if fn.get("arguments"):
                                    tcs[i]["arguments"] += fn["arguments"]
                            if choice.get("finish_reason"):
                                res.finish_reason = choice["finish_reason"]
                        res.tool_calls = tcs
            except _Retry as r:
                failure = r
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                failure = _Retry("down", f"{type(e).__name__}: {e}")
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.WriteError) as e:
                failure = _Retry("dropped" if res.emitted else "transient", f"{type(e).__name__}: {e}")

            if failure is None:
                # --- text-format tool calls ("Action: tool(...)"): recover instead of losing the turn ---
                rest = tcf.finish()
                if tcf.tripped and not any(tc["name"] for tc in res.tool_calls):
                    recovered = tcf.calls()
                    if recovered:
                        logger.warning("Model wrote its tool call as text; recovered: "
                                       + ", ".join(c["name"] for c in recovered))
                        res.tool_calls, res.finish_reason = recovered, "tool_calls"
                        res.content, rest = tcf.visible_text, ""
                    else:
                        logger.warning("Unparseable text tool call shown as text: " + tcf.held_text[:200])
                        rest = tcf.held_text
                if not res.degenerate:
                    rest = (st.guard.feed(rest) if rest else "") + st.guard.flush()
                else:
                    rest = ""
                res.raw = res.content
                if st.guard.dropped > dropped_before:
                    res.content = st.guard.round_text()               # what the user actually saw, for the history
                    self._trace(event="repeat_dropped", round=st.rounds, n=st.guard.dropped - dropped_before)
                if rest and hold:
                    res.held += rest
                    res.emitted = True
                elif rest:
                    out = self._sep(st, rest) if first_visible else rest
                    res.emitted = True
                    self._note_emitted(st, out)
                    yield Event("content", out)
                if res.finish_reason is None and not res.emitted and not any(t["name"] for t in res.tool_calls):
                    failure = _Retry("transient", "stream ended without a reply")     # connection dropped silently
                else:
                    yield Event("_round", res)
                    return

            # ------------------------------ failure handling ------------------------------
            self._trace(event="llm_failure", kind=failure.kind, status=failure.status, detail=failure.detail[:200])
            if failure.kind in ("down", "loading"):
                down_tries += 1
                waited_since = waited_since or time.monotonic()
                if down_tries > 3:
                    yield Event("error", {"error": "Failed to connect to brain", "detail": failure.detail[:300]})
                    yield Event("_round", None)
                    return
                if failure.kind == "down" and not self.brain.loading():
                    await self.brain.ensure()                   # crashed / never started: start it
                    await asyncio.sleep(0.3)
                yield Event("activity", f"load:{self.brain.label}")
                while time.monotonic() - waited_since < cfg.ready_wait_s:
                    if await self.brain.ready():
                        break
                    if self.brain.error() and not self.brain.loading():
                        break
                    await asyncio.sleep(1.5)
                if await self.brain.ready():
                    st.retries += 1
                    yield Event("activity", "think")
                    continue
                yield Event("error", {"error": "Failed to connect to brain",
                                      "detail": self.brain.error() or f"{self.brain.label} did not become ready"})
                yield Event("_round", None)
                return
            if failure.kind == "context" and not st.context_retry:
                st.context_retry = True
                st.retries += 1
                self._prune(messages, aggressive=True)
                continue
            if failure.kind == "transient" and attempt <= cfg.http_retries:
                st.retries += 1
                await asyncio.sleep(1.5 * attempt)
                continue
            if failure.kind == "fatal" and payload.get("tool_choice") == "required" and not st.required_broken:
                st.required_broken, st.force_tool = True, False       # this server can't force it: nudges only
                self._trace(event="required_unsupported", status=failure.status)
                continue
            msg = {"error": "Failed to connect to brain", "detail": failure.detail[:500]}
            if failure.status:
                msg["status"] = failure.status
            if failure.kind == "dropped":
                msg["error"] = "Connection to the brain dropped mid-reply"
            yield Event("error", msg)
            yield Event("_round", None)
            return

    @staticmethod
    def _classify(status: int, body: str) -> str:
        low = body.lower()
        if status == 503 or "loading model" in low:
            return "loading"
        if status in (400, 413) and any(k in low for k in ("context", "exceed", "too long", "n_ctx", "n_keep")):
            return "context"
        if status >= 500 or status == 429:
            return "transient"
        return "fatal"

    # =========================================================================== tool calls
    def _normalize_calls(self, res: RoundResult, st: TurnState) -> list[dict]:
        calls, seen = [], set()
        for i, tc in enumerate(res.tool_calls):
            if not tc.get("name"):
                continue
            cid = tc.get("id") or ""
            if not cid or cid in seen:
                cid = f"call_{st.rounds}_{i}"
            seen.add(cid)
            calls.append({"id": cid, "name": tc["name"].strip(), "arguments": tc.get("arguments", "")})
        return calls

    async def _run_tools(self, res: RoundResult, calls: list[dict], messages: list[dict],
                         st: TurnState) -> AsyncIterator[Event]:
        prepared = []
        for c in calls:
            args, err = parse_args(c["arguments"])
            c["args"], c["parse_error"] = args, err
            c["arguments"] = json.dumps(args if args is not None else {}, ensure_ascii=False)   # keep history valid
            prepared.append(c)
        messages.append({"role": "assistant", "content": res.content, "tool_calls": [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
            for c in prepared]})
        st.tool_calls_total += len(prepared)

        i = 0
        while i < len(prepared):
            group = self._parallel_group(prepared, i)
            if len(group) > 1:
                first = group[0]
                yield Event("activity", self.activity_for(first["name"], first["args"] or {}))

                async def timed(c):
                    t0 = time.monotonic()
                    out = await self._execute_one(c, st)
                    return out, int((time.monotonic() - t0) * 1000)

                async def run_all():
                    return await asyncio.gather(*[timed(c) for c in group])

                outcomes: list = []
                async for item in self._with_side_events(run_all()):
                    if isinstance(item, Event):
                        yield item
                    else:
                        outcomes = item
                self._trace(event="parallel_reads", round=st.rounds, tools=[c["name"] for c in group])
                for c, ((result, ok, flag), ms) in zip(group, outcomes):      # results go back in the order they were asked
                    async for ev in self._record(c, result, ok, flag, ms, messages, st):
                        yield ev
                i += len(group)
                continue
            c = prepared[i]
            name = c["name"]
            yield Event("activity", "think" if name in (PLAN_TOOL_NAME, TOOL_SEARCH_NAME) else self.activity_for(name, c["args"] or {}))
            t0 = time.monotonic()
            result, ok, flag = "", False, ""
            async for item in self._with_side_events(self._execute_one(c, st)):
                if isinstance(item, Event):
                    yield item                       # e.g. an approval card while the call waits for the user
                else:
                    result, ok, flag = item
            ms = int((time.monotonic() - t0) * 1000)
            async for ev in self._record(c, result, ok, flag, ms, messages, st):
                yield ev
            i += 1

    def _parallel_group(self, prepared: list, start: int) -> list:
        """Consecutive independent read-only calls starting at `start` that may run at the same time (LUCY_PARALLEL=0 = never)."""
        if os.environ.get("LUCY_PARALLEL", "1").strip().lower() in ("0", "false", "off", "no"):
            return []
        group, seen = [], set()
        for c in prepared[start:]:
            sig = (c["name"], json.dumps(c["args"], sort_keys=True, default=str))
            if c["name"] not in PARALLEL_TOOLS or c["name"] not in self.schemas or c["parse_error"] or sig in seen:
                break
            seen.add(sig)
            group.append(c)
        return group

    async def _record(self, c: dict, result: str, ok: bool, flag: str, ms: int, messages: list, st: TurnState) -> AsyncIterator[Event]:
        name = c["name"]
        self._trace(event="tool", round=st.rounds, tool=name, ok=ok, flag=flag, ms=ms,
                    result_chars=len(result), args=json.dumps(c["args"] or {}, ensure_ascii=False)[:300],
                    error=("" if ok else result[:240]))
        if ok:
            st.ok_tools.add(name)
            if name in DELIVERS_FILES and result.startswith("MEDIA:"):
                st.ok_tools.add("send_file")
        model_view = result
        if result.startswith("MEDIA:"):
            # The UI shows the file; the model only needs to know it was delivered (and must not echo the path)
            short = json.dumps(c["args"] or {}, ensure_ascii=False)[:120]
            model_view = (f"Delivered to the user's chat via {name} {short}. It is displayed automatically - do "
                          "not write the file path or a MEDIA: line in your reply.")
        messages.append({"role": "tool", "tool_call_id": c["id"], "content": model_view})
        st.last_result = model_view
        if result.startswith("MEDIA:"):
            yield Event("media", result.split(" ", 1)[0].replace("MEDIA:", ""))

    async def _with_side_events(self, coro):
        """Run `coro` and stream out the events it emits while it runs (the gate's approval cards), then its result.
        If the consumer stops (Stop button) the call is cancelled, which also withdraws a pending approval."""
        q: asyncio.Queue = asyncio.Queue()
        self._emit = lambda kind, data=None: q.put_nowait(Event(kind, data))
        task = asyncio.ensure_future(coro)
        getter = None
        try:
            while True:
                getter = asyncio.ensure_future(q.get())
                done, _ = await asyncio.wait({task, getter}, return_when=asyncio.FIRST_COMPLETED)
                if getter in done:
                    yield getter.result()
                else:
                    getter.cancel()
                getter = None
                if task.done():
                    while not q.empty():
                        yield q.get_nowait()
                    yield task.result()
                    return
        finally:
            self._emit = None
            if getter is not None:
                getter.cancel()
            if not task.done():
                task.cancel()

    # What to try when a tool is blocked. The loop never gives up on a task because a tool failed: it hands
    # the model concrete ways round the problem, escalating the longer the blocker lasts.
    _PLAYBOOK = {
        "edit_local_file": "read_local_file the exact lines (use search / start_line) and retry with a smaller, exact "
                           "old_string; or rewrite the whole file with write_local_file; or apply the change with "
                           "run_shell_command (a short Python script or sed). Binary formats (.docx .xlsx .pptx .pdf) cannot "
                           "be text-edited: use docx_edit / xlsx_edit / pptx_edit (office_read the file first) or docx_create / "
                           "xlsx_create / pptx_create / pdf_create for a new one.",
        "write_local_file": "check the folder with list_local_files, try a path inside the Lucy Core folder, or write "
                            "the file with run_shell_command (a short Python script).",
        "read_local_file": "find the right path with find_file / list_local_files, read a slice with search or "
                           "start_line/end_line, or use run_shell_command as a fallback.",
        "send_file": "confirm the real path with find_file / list_local_files; a folder must be zipped first "
                     "(run_shell_command) and the zip sent; a file outside the allowed folder must be copied into the "
                     "Lucy Core folder first (run_shell_command) and the copy sent.",
        "apply_patch": "read_local_file the exact lines and retry with smaller edits whose old text is copied exactly; or use "
                       "edit_local_file for one change at a time; or rewrite the whole file with write_local_file.",
        "run_tests": "read the failure lines it gave you, open the failing code with read_local_file, fix it, and run it again; "
                     "if no framework was found, pass cwd (the folder with the tests) or use run_shell_command.",
        "docx_create": "read the error: it names the block that is wrong. Retry with 'markdown' (headings, - bullets, | tables |) instead of 'blocks'.",
        "docx_edit": "office_read the file and copy the exact words into find / match (a shorter piece is safer); tables are edited with set_cell / add_row.",
        "xlsx_create": "read the error, then retry with 'columns' (header texts) and 'rows' (lists of cells); formulas are texts starting with '='.",
        "xlsx_edit": "office_read the file to see the sheet names and cells, then retry; use sheet 1, 2, 3 if a name does not match.",
        "pptx_create": "retry with 'markdown': '# Title' and '- bullets' for each slide, slides separated by a line '---'.",
        "pptx_edit": "office_read the deck to see the slide numbers (from 1), then retry with a smaller list of ops.",
        "pdf_create": "retry with 'markdown'; if characters are missing, make the document with docx_create instead.",
        "office_read": "check the path with find_file; for a scanned PDF use office_render and analyze_image; for an old .doc / .xls LibreOffice must be installed.",
        "office_render": "LibreOffice is needed for Word / Excel / PowerPoint (PDFs work without): tell the user, and check the content with office_read instead.",
        "git": "run git action 'status' to see the real state first; if it is not a repository use action 'init'; "
               "anything else (push, pull, reset) goes through run_shell_command and may ask the user.",
        "code_search": "try a shorter or different query, regex=true, or a wider folder; or find the file with find_file.",
        "project_map": "pass a different path, or use list_local_files and read_local_file instead.",
        "python": "send smaller code; for files or programs use the file tools or run_shell_command (the python tool cannot).",
        "format_and_lint": "no linter may be installed: skip it and tell the user, or check the syntax by running the code.",
        "web_search": "rephrase with different keywords, search for a more specific part of the question, or open a "
                      "known page with web_extract.",
        "web_extract": "try another result from web_search, or search for a cached / alternative source of the page.",
        "run_shell_command": "check the syntax for this operating system, split the command into smaller steps, or do "
                             "it with a short Python script (run_shell_command python -c ...).",
        "create_google_doc": "the Google connection may be unavailable: write the content to a local file with "
                             "write_local_file and deliver it with send_file instead.",
        "list_drive_files": "the Google connection may be unavailable: work with local files instead.",
        "get_doc_content": "the Google connection may be unavailable: ask for the content or use a local copy.",
    }

    def _workaround(self, name: str, streak: int) -> str:
        if streak < 2:
            return ""
        how = self._PLAYBOOK.get(name, "reach the same goal with a different tool or a different method")
        text = (f"\n{_NOTE}Blocked: {name} has failed {streak} times in a row. Do NOT stop and do NOT repeat the "
                f"same call. Work around it - {how}")
        if streak >= 4:
            text += (" Keep going until the task is done. Only after you have tried at least two genuinely "
                     "different approaches may you tell the user what is blocking you - and even then finish every "
                     "part of the task that you can.")
        return text

    async def _execute_one(self, c: dict, st: TurnState) -> tuple[str, bool, str]:
        name, args = c["name"], c["args"]
        cfg = self.cfg

        def fail(text: str, flag: str) -> tuple[str, bool, str]:
            st.tool_errors += 1
            st.fail_streak[name] = st.fail_streak.get(name, 0) + 1
            return text + self._workaround(name, st.fail_streak[name]), False, flag

        if c["parse_error"]:
            return fail(f"Invalid arguments for {name}: {c['parse_error']}. Nothing was run. "
                        "Call it again with a valid JSON object.", "bad_json")
        if name not in self.schemas and self.tool_catalog is not None and name != TOOL_SEARCH_NAME:
            spec = self.tool_catalog.get(name)                # a real tool that just wasn't shown this turn: add it, run it
            if spec is not None and self._add_tools([spec]):
                self._trace(event="tool_auto_added", round=st.rounds, tool=name)
        if name not in self.schemas:
            close = difflib.get_close_matches(name, list(self.schemas), n=3, cutoff=0.5)
            return fail(f"Unknown tool '{name}'. Nothing was run. "
                        + (f"Did you mean: {', '.join(close)}? " if close else "")
                        + f"Available tools: {', '.join(sorted(self.schemas))}.", "unknown_tool")
        args = coerce_args(args, self.schemas[name])
        c["args"] = args
        missing = [k for k in (self.schemas[name].get("required") or []) if args.get(k) is None]
        if missing:
            return fail(f"Missing required argument(s) for {name}: {', '.join(missing)}. Nothing was run. "
                        "Call it again with all required arguments.", "missing_args")

        if name == TOOL_SEARCH_NAME:
            st.search_updates += 1
            query = str(args.get("query") or "")
            found = self.tool_catalog.search(query) if self.tool_catalog is not None else []
            added = self._add_tools(found)
            self._trace(event="tool_search", round=st.rounds, query=query[:80], found=[t["function"]["name"] for t in found], added=added)
            if found:
                more = getattr(self.tool_catalog, "more_hint", None)
                more = more(found) if callable(more) else ""
                return (f"Now available (call them directly):\n{describe_tools(found)}\n"
                        + (more + "\n" if more else "")
                        + f"{_NOTE}Use the right one for the user's request now."), True, ""
            have = ", ".join(sorted(n for n in self.schemas if n != TOOL_SEARCH_NAME))
            return f"No tool matches '{query}'. Tools you already have: {have}.", True, ""

        if name == PLAN_TOOL_NAME:
            err = st.plan.set(args.get("steps"))
            if err:
                return fail(f"Invalid plan: {err}. Nothing was changed.", "bad_plan")
            nxt = st.plan.next_step()
            tail = (f"Next: {nxt}. Do it now by calling its tool (don't just say you will)." if nxt
                    else "All steps are finished. Give the user your final answer.")
            return f"Plan saved:\n{st.plan.render()}\n{_NOTE}{tail}", True, ""

        sig = (name, json.dumps(args, sort_keys=True, default=str))
        key = sig
        if name in READ_ONLY_TOOLS and key in st.cache and st.cache[key][1] == st.epoch:
            st.repeats += 1
            push = ("You keep re-reading the same thing: you already HAVE this information. Use it to do the next "
                    "step of the task now (edit, write, send...) instead of reading again."
                    if st.repeats >= cfg.max_repeat_calls else
                    "Don't repeat it - use the result to continue with the task, or try something different.")
            return (f"{_NOTE}You already ran this exact call this turn; reusing its result. {push}\n"
                    f"{st.cache[key][0]}"), True, "repeat"
        prev = st.sig_failed.get(sig)
        if prev and prev[0] >= 2 and prev[1] == st.epoch:
            return fail(f"This exact call already failed {prev[0]} times with: {prev[2]}\n{_NOTE}It was not run "
                        "again, because nothing has changed since. Change the arguments or the approach.", "repeat_fail")

        if self.gate is not None:
            async with self._gate_lock:                 # parallel calls: ask about one at a time
                verdict = await self.gate(name, args, self._emit)
            if verdict is not None:                     # denied / refused / nobody to approve: never a dead end
                st.tool_errors += 1                     # ...but no workaround playbook: getting the same effect through
                st.sig_failed[sig] = (2, st.epoch, verdict[:300])   # another tool would defeat the user's decision
                self._trace(event="tool_denied", round=st.rounds, tool=name, reason=verdict[:200])
                return verdict, False, "denied"
        try:
            raw = await asyncio.wait_for(self.execute(name, args), timeout=cfg.tool_timeout)
        except asyncio.TimeoutError:
            st.sig_failed[sig] = ((prev[0] if prev else 0) + 1, st.epoch, "timed out")
            return fail(f"Tool timed out after {int(cfg.tool_timeout)}s: {name}. "
                        "Try a smaller or different request.", "timeout")
        except asyncio.CancelledError:
            raise
        except Exception as e:                              # noqa: BLE001 - a tool must never kill the turn
            logger.exception(f"Tool {name} raised")
            msg = f"Tool failed: {name} raised {type(e).__name__}: {e}"
            st.sig_failed[sig] = ((prev[0] if prev else 0) + 1, st.epoch, msg[:300])
            return fail(msg, "exception")

        result = "(no output)" if raw is None else raw if isinstance(raw, str) else json.dumps(raw, default=str)
        result = self._cap(result)
        if looks_failed(result):
            if name == "edit_local_file" and "not found in file" in result:
                result += (f"\n{_NOTE}Re-read the file with read_local_file and copy old_string exactly, "
                           "including whitespace and line breaks.")
            st.sig_failed[sig] = ((prev[0] if prev else 0) + 1, st.epoch, result[:300])
            return fail(result, "tool_error")
        st.fail_streak[name] = 0
        st.progress = True
        if self.verify is not None and name in VERIFY_NAMES:      # harness-side check of the changed file (no approval, can't be skipped)
            try:
                extra = await self.verify(name, args, result)
            except asyncio.CancelledError:
                raise
            except Exception as e:                              # noqa: BLE001 - a check must never break the turn
                logger.warning(f"verify after {name} failed: {e}")
                extra = ""
            if extra:
                result += "\n" + extra
                self._trace(event="verify", round=st.rounds, tool=name, note=extra[:160])
        if name in READ_ONLY_TOOLS:
            st.cache[key] = (result, st.epoch)
        else:
            st.epoch += 1
        return result, True, ""

    def _cap(self, text: str) -> str:
        cap = self.cfg.max_result_chars
        if len(text) <= cap:
            return text
        head, tail = int(cap * 0.7), int(cap * 0.3)
        omitted = len(text) - head - tail
        return (f"{text[:head]}\n[... {omitted} characters omitted to protect the context window; ask for a "
                f"narrower slice or a more specific query if you need them ...]\n{text[-tail:]}")

    # =========================================================================== replies
    def _judge_final(self, res: RoundResult, messages: list[dict], st: TurnState,
                     final_round: bool) -> tuple[str, str]:
        """No tool calls this round. Decide: done / retry (messages were extended) / fallback text.

        A plain reply only ends the turn when nothing is left undone. Every "not yet" below pushes the model on
        (and from the 3rd push the next round MUST call a tool) instead of giving up."""
        cfg = self.cfg
        text = res.raw or res.content          # judge what the model meant, even if repeated sentences were hidden
        hist = res.content if res.content.strip() else text      # what goes into the conversation history
        if res.finish_reason == "length" and text.strip() and st.continues < cfg.max_continuations:
            st.continues += 1
            st.glue = True
            st.release_held = True
            messages.append({"role": "assistant", "content": hist})
            messages.append({"role": "user", "content": _NOTE + "Your reply was cut off by the output limit. "
                             "Continue exactly where you stopped, without repeating anything."})
            self._trace(event="continue", n=st.continues)
            return "retry", ""
        if not text.strip():
            if st.empty_retries < cfg.max_empty_retries:
                st.empty_retries += 1
                messages.append({"role": "user", "content": _NOTE + "Your last reply was empty. " + (
                    "Use the tool results above to continue the task: call the next tool, or if everything is "
                    "done, answer the user in plain text." if st.tool_calls_total
                    else "Answer the user's last message in plain text.")})
                self._trace(event="empty_reply_retry", n=st.empty_retries)
                return "retry", ""
            if st.emitted_any:
                return "done", ""
            if st.last_result:
                return "fallback", ("I ran the tools but couldn't put a proper reply together. The last result was:\n\n"
                                    + st.last_result[:800])
            return "fallback", "I didn't get a reply from the model. Please try again."
        if cfg.narration_guard and st.claim_nudges < 2 and self.tools:
            claim = unbacked_claim(text, st.ok_tools)
            if claim:
                sentence, tools = claim
                st.claim_nudges += 1
                names = "/".join(sorted(tools))
                can = "Make the tool call now, or " if not final_round else "You cannot call tools any more this turn, so "
                messages.append({"role": "assistant", "content": hist})
                messages.append({"role": "user", "content": _NOTE + (
                    f"You said: \"{sentence[:140]}\" - but no {names} call succeeded this turn, so that did not "
                    f"happen. {can}correct your statement honestly. (If it really was done in an earlier turn, say "
                    "that briefly.) Never report an action as done without its tool result.")})
                self._trace(event="unbacked_claim", sentence=sentence[:140])
                return "retry", ""
        promise = find_promise(text) if cfg.narration_guard and not final_round and self.tools else None
        if promise and "remember" in st.ok_tools and re.search(r"remember|note|save|store|record", text, re.IGNORECASE):
            promise = None                                     # "I'll remember that" after remember() already ran
        if promise and ({"set_reminder", "schedule_task"} & st.ok_tools) and re.search(r"remind|schedul|follow", text, re.IGNORECASE):
            promise = None                                     # "I'll remind you" after set_reminder() already ran
        if promise:
            return self._broken_promise(promise, hist, messages, st)
        pending = st.plan.pending()
        if pending and not final_round and self.tools:
            st.plan.nudges += 1
            st.nudges += 1
            messages.append({"role": "assistant", "content": hist})
            messages.append({"role": "user", "content": _NOTE + (
                "Your plan still has unfinished steps:\n" + st.plan.render() + "\n"
                f"Do the next one now: {pending[0]['text']}. Call its tool; don't just describe it. If a step is no "
                "longer needed, mark it skipped with update_plan. Only answer once every step is done or skipped.")})
            if st.plan.nudges > cfg.max_plan_nudges:
                st.force_tool = True
            self._trace(event="plan_nudge", pending=len(pending), forced=st.force_tool)
            return "retry", ""
        # The user explicitly asked for something a tool must do (send a file, edit a file) and none did.
        if st.wanted and not final_round and self.tools and st.cover_nudges < cfg.max_cover_nudges \
                and not text.rstrip().endswith("?"):
            todo = [(label, tools) for label, tools in st.wanted
                    if not (tools & st.ok_tools) and any(t in self.schemas for t in tools)]
            if todo:
                st.cover_nudges += 1
                st.nudges += 1
                label, tools = todo[0]
                names = " / ".join(sorted(t for t in tools if t in self.schemas))
                messages.append({"role": "assistant", "content": hist})
                messages.append({"role": "user", "content": _NOTE + (
                    f"The user asked you to {label}, but no {names} call has succeeded this turn, so that is not "
                    "done yet. Do it now with the tool. If something blocks you, work around it (another path, "
                    "another method) instead of stopping; if the request was truly already satisfied, say so "
                    "in one sentence.")})
                self._trace(event="cover_nudge", wanted=label)
                return "retry", ""
        return "done", ""

    def _broken_promise(self, promise: Promise, text: str, messages: list[dict], st: TurnState) -> tuple[str, str]:
        """The reply ended with "I'll do X" and no tool call. Never let the turn die on that:

          1st time  nudge            - tell the model to call the tool now
          2nd time  auto-act         - if it announced a web search, the harness runs it (queries from the text,
                                       else the user's last message); otherwise a firmer nudge
          3rd+ time REQUIRED         - the next round must contain a tool call (tool_choice=required); finish_task
                                       is offered as the exit for a model that really is done
        """
        cfg = self.cfg
        st.promise_streak += 1
        n = st.promise_streak
        self._trace(event="broken_promise", n=n, kind=promise.kind, sentence=promise.sentence[:140])
        local = bool(_LOCAL_HINT.search(text))        # "search for repeat_guard.py in the folder" is NOT a web search
        can_auto = (promise.kind == "search" and "web_search" in self.schemas and st.auto_acts < cfg.max_auto_acts
                    and not local)
        if n >= 2 and can_auto:
            queries = promise.queries or [self._fallback_query(messages)]
            st.auto_calls = [{"id": f"auto_{st.rounds}_{i}", "name": "web_search", "parse_error": None,
                              "args": {"query": q}, "arguments": json.dumps({"query": q}, ensure_ascii=False)}
                             for i, q in enumerate([q for q in queries if q][:3])]
            if st.auto_calls:
                return "auto", ""
        st.nudges += 1
        firm = "" if n == 1 else (f" This is the {'second' if n == 2 else str(n) + 'th'} time you announced an action "
                                  "and then stopped. Your NEXT message must be the tool call itself, with no text "
                                  "before it.")
        messages.append({"role": "assistant", "content": text})
        messages.append({"role": "user", "content": _NOTE + (
            "You said you would take an action but did not call a tool. Call the tool now - don't "
            "describe it. If no tool is needed because the task is finished, just give the final answer." + firm)})
        if n > cfg.max_promise_nudges:
            st.force_tool = True
        return "retry", ""

    @staticmethod
    def _fallback_query(messages: list[dict]) -> str:
        """Last real user message (not a harness note), trimmed - the best query when none was named."""
        for m in reversed(messages):
            c = m.get("content")
            if m.get("role") == "user" and isinstance(c, str) and not c.startswith(_NOTE) and c.strip():
                return re.sub(r"\s+", " ", c).strip()[:150]
        return ""

    # =========================================================================== context budget
    def _msg_tokens(self, m: dict) -> int:
        c, n = m.get("content"), 6
        if isinstance(c, list):
            for part in c:
                n += self.count(part.get("text", "")) if part.get("type") == "text" else 1500
        elif c:
            n += self.count(c)
        for tc in m.get("tool_calls") or []:
            n += self.count(tc["function"]["arguments"]) + 8
        return n

    def _prune(self, messages: list[dict], aggressive: bool = False) -> None:
        """Shrink the conversation if it is close to the context limit (oldest tool output first)."""
        cfg = self.cfg
        budget = int(cfg.ctx_tokens * cfg.ctx_safety) - cfg.max_tokens - self._tools_tokens
        if aggressive:
            budget = int(budget * 0.6)
        approx = sum(len(m.get("content") or "") if isinstance(m.get("content"), str) else 6000 for m in messages) / 3.5
        if not aggressive and approx < budget * 0.5:
            return                                           # clearly fine: skip precise counting
        total = sum(self._msg_tokens(m) for m in messages)
        if total <= budget:
            return
        before = total
        tool_idx = [i for i, m in enumerate(messages) if m["role"] == "tool"]
        keep = set(tool_idx[-1:]) if not aggressive else set()
        for i in tool_idx:                                    # 1) stub old tool results
            if total <= budget:
                break
            m = messages[i]
            if i in keep or len(m["content"]) < 600:
                continue
            saved = self._msg_tokens(m)
            m["content"] = m["content"][:200].rstrip() + "\n[... older tool result removed to save context ...]"
            total -= saved - self._msg_tokens(m)
        for i, m in enumerate(messages):                      # 2) shorten long old chat messages
            if total <= budget:
                break
            if m["role"] in ("user", "assistant") and isinstance(m.get("content"), str) \
                    and len(m["content"]) > 3000 and i < len(messages) - 4:
                saved = self._msg_tokens(m)
                m["content"] = m["content"][:1500] + "\n[... shortened ...]\n" + m["content"][-500:]
                total -= saved - self._msg_tokens(m)
        i = 0                                                 # 3) drop the oldest non-system messages
        while total > budget and len(messages) > 6:
            while i < len(messages) and messages[i]["role"] == "system":
                i += 1
            if i >= len(messages) - 4:
                break
            drop = [i]
            if messages[i].get("tool_calls"):                 # keep assistant/tool pairs together
                j = i + 1
                while j < len(messages) and messages[j]["role"] == "tool":
                    drop.append(j)
                    j += 1
            elif messages[i]["role"] == "tool":
                j = i + 1
                while j < len(messages) and messages[j]["role"] == "tool":
                    drop.append(j)
                    j += 1
            for j in reversed(drop):
                total -= self._msg_tokens(messages[j])
                del messages[j]
        logger.warning(f"Context pruned: ~{before} -> ~{total} tokens (budget {budget})")
        self._trace(event="prune", before=before, after=total, budget=budget)
