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
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx

from lucy.server.multistep import PLAN_TOOL, PLAN_TOOL_NAME, Plan, Promise, find_promise
from lucy.server.text_tool_calls import TextToolCallFilter

logger = logging.getLogger("lucy.core.agent")

# Tools that only read: an identical repeat within the same state of the world is wasted work.
READ_ONLY_TOOLS = frozenset({
    "web_search", "web_extract", "read_local_file", "list_local_files", "find_file", "recall_memory",
    "search_conversations", "get_doc_content", "list_drive_files", "analyze_image",
})

_NOTE = "[Harness note - internal, do not mention or quote it] "

# "I saved it to memory" / "I updated api.py" / "I sent the sticker" / "I searched the web" - said without the
# tool call that would make it true. (verbs, objects, tools that would back the claim)
_CLAIM_FAMILIES = [
    ("saved something to memory", r"saved|stored|added|noted|recorded|committed|written|logged|remembered",
     r"memory|persistent|long[- ]term|remember", {"remember"}),
    ("changed a file", r"updated|edited|patched|modified|changed|rewrote|written|wrote|created|saved|fixed|appended|added",
     r"file|\.py\b|\.md\b|\.json\b|api\.py|lucy\.md|\bcode\b|persona|harness|script", {"edit_local_file", "write_local_file"}),
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


_FAIL_PREFIXES = ("error", "file not found", "directory not found", "image not found", "tool failed",
                  "tool timed out", "unknown tool", "invalid arguments", "missing required")


@dataclass
class Event:
    kind: str                   # content | activity | media | error | metrics
    data: Any = None


@dataclass
class LoopConfig:
    max_rounds: int = 8             # tool rounds per turn (a last tool-less round always follows)
    max_tokens: int = 4096          # per model call
    tool_timeout: float = 120.0     # seconds per tool call
    max_result_chars: int = 12000   # cap on one tool result fed back to the model
    ctx_tokens: int = 100000        # llama-server context size
    ctx_safety: float = 0.75        # token counts are estimates; leave headroom
    http_timeout: float = 600.0
    connect_timeout: float = 5.0
    ready_wait_s: float = 300.0     # how long to wait for a loading / crashed model
    http_retries: int = 2
    max_empty_retries: int = 2
    max_continuations: int = 2
    max_repeat_calls: int = 3       # identical read-only repeats before forcing an answer
    max_tool_errors: int = 8        # failed tool calls in one turn before forcing an answer
    narration_guard: bool = True
    max_promise_nudges: int = 2     # broken promises in a row: nudge, then (2nd) run the search ourselves, then give up
    max_auto_acts: int = 2          # times per turn the harness may run an announced search itself
    plan_tool: bool = True          # offer the update_plan checklist tool
    max_plan_nudges: int = 2        # "your plan still has pending steps" reminders per turn


@dataclass
class RoundResult:
    content: str = ""               # everything the model said (visible text)
    tool_calls: list = field(default_factory=list)      # [{"id","name","arguments"(str)}]
    finish_reason: str | None = None
    emitted: bool = False


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
    """

    def __init__(self, *, brain, tools: list[dict], execute: Callable[[str, dict], Awaitable[str]],
                 activity_for: Callable[[str, dict], str], count_tokens: Callable[[str], int],
                 temperature: float, cfg: LoopConfig | None = None,
                 trace: Callable[[dict], None] | None = None, session_id: str | None = None):
        self.cfg = cfg or LoopConfig()
        if self.cfg.plan_tool and not any(t.get("function", {}).get("name") == PLAN_TOOL_NAME for t in tools):
            tools = list(tools) + [PLAN_TOOL]
        self.brain, self.tools, self.execute = brain, tools, execute
        self.activity_for, self.count, self.temperature = activity_for, count_tokens, temperature
        self._trace_cb, self.session_id = trace, session_id
        self.schemas = {t["function"]["name"]: t["function"].get("parameters", {}) or {}
                        for t in tools if t.get("function", {}).get("name")}
        try:
            self._tools_tokens = self.count(json.dumps(tools))
        except Exception:
            self._tools_tokens = 4000

    def _trace(self, **kw) -> None:
        if self._trace_cb:
            try:
                self._trace_cb({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "session": self.session_id, **kw})
            except Exception:
                pass

    # =========================================================================== main loop
    async def run(self, messages: list[dict]) -> AsyncIterator[Event]:
        cfg, st = self.cfg, TurnState()
        t0 = time.monotonic()
        finish = "ok"
        yield Event("activity", "think")
        hard_cap = (cfg.max_rounds + 1 + cfg.max_empty_retries + cfg.max_continuations + cfg.http_retries + 4
                    + cfg.max_promise_nudges + cfg.max_auto_acts + cfg.max_plan_nudges)
        while st.rounds < hard_cap:
            st.rounds += 1
            final_round = st.force_final or st.rounds > cfg.max_rounds
            if final_round and not st.force_final:
                st.force_final = True
                messages.append({"role": "user", "content": _NOTE + (
                    "You have used all your tool calls for this turn and cannot call any more. Do not fill "
                    "remaining gaps with assumptions or guesses. Report only what you actually confirmed via "
                    "tools. For anything you did not confirm, say plainly that you don't know yet and ask "
                    "the user, rather than presenting a guess as fact.")})
            self._prune(messages)
            # Every model call is announced. Retries (nudges, auto-continue, empty-reply retries) used to run
            # silently, and the UI hides its indicator once text streams in - so those rounds looked frozen.
            yield Event("activity", "think")

            res: RoundResult | None = None
            async for ev in self._round(messages, [] if final_round else self.tools, st):
                if ev.kind == "_round":
                    res = ev.data
                else:
                    yield ev
            st.glue = False
            if res is None:                              # fatal error already reported
                finish = "error"
                break

            calls = [] if final_round else self._normalize_calls(res, st)
            if calls:
                if res.finish_reason == "length" and any(parse_args(c["arguments"])[0] is None for c in calls):
                    st.truncated_calls += 1
                    self._trace(event="truncated_tool_call", round=st.rounds)
                    if res.content.strip():
                        messages.append({"role": "assistant", "content": res.content})
                    messages.append({"role": "user", "content": _NOTE + (
                        "Your tool call was cut off by the output limit, so it was NOT run. Redo it with a much "
                        "smaller payload (for a large file, create it small and add the rest in several "
                        "edit_local_file steps).")})
                    if st.truncated_calls > 2:
                        st.force_final = True
                    continue
                async for ev in self._run_tools(res, calls, messages, st):
                    yield ev
                st.promise_streak = 0                    # it acted: the last promise (if any) was kept
                if all(c["name"] == PLAN_TOOL_NAME for c in calls) and st.plan.updates <= 4:
                    st.rounds -= 1                       # bookkeeping isn't work: don't spend a tool round on it
                yield Event("activity", None)
                yield Event("activity", "think")
                if st.repeats >= cfg.max_repeat_calls or st.tool_errors >= cfg.max_tool_errors:
                    st.force_final = True
                    finish = "forced"
                continue

            verdict, extra = self._judge_final(res, messages, st, final_round)
            if verdict == "retry":
                continue
            if verdict == "auto":                        # the model keeps announcing a search: run it for it
                auto, st.auto_calls = st.auto_calls, []
                st.auto_acts += 1
                self._trace(event="auto_act", round=st.rounds, calls=[(c["name"], c["arguments"][:120]) for c in auto])
                async for ev in self._run_tools(res, auto, messages, st):
                    yield ev
                yield Event("activity", None)
                yield Event("activity", "think")
                continue
            if verdict == "fallback":
                yield Event("content", self._sep(st, extra))
                st.emitted_any, st.last_char = True, extra[-1:]
            break

        self._trace(event="turn", rounds=st.rounds, tool_calls=st.tool_calls_total, tool_errors=st.tool_errors,
                    nudges=st.nudges, auto_acts=st.auto_acts, plan_steps=len(st.plan.steps), empty_retries=st.empty_retries, continues=st.continues,
                    retries=st.retries, repeats=st.repeats, finish=finish,
                    ms=int((time.monotonic() - t0) * 1000))
        yield Event("metrics", {"rounds": st.rounds, "tool_calls": st.tool_calls_total,
                                "tool_errors": st.tool_errors, "retries": st.retries})

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
            payload = {"model": brain.model, "messages": messages, "temperature": self.temperature,
                       "max_tokens": cfg.max_tokens, "tools": tools, "stream": True,
                       "stream_options": {"include_usage": True}, **brain.extras()}
            if tools:
                payload["tool_choice"] = "auto"
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
                                    out = self._sep(st, piece) if first_visible else piece
                                    first_visible = False
                                    res.emitted = True
                                    self._note_emitted(st, out)
                                    yield Event("content", out)
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
                if rest:
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

        for c in prepared:
            name = c["name"]
            yield Event("activity", "think" if name == PLAN_TOOL_NAME else self.activity_for(name, c["args"] or {}))
            t0 = time.monotonic()
            result, ok, flag = await self._execute_one(c, st)
            ms = int((time.monotonic() - t0) * 1000)
            self._trace(event="tool", round=st.rounds, tool=name, ok=ok, flag=flag, ms=ms,
                        result_chars=len(result), args=json.dumps(c["args"] or {}, ensure_ascii=False)[:300])
            if ok:
                st.ok_tools.add(name)
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

    async def _execute_one(self, c: dict, st: TurnState) -> tuple[str, bool, str]:
        name, args = c["name"], c["args"]
        cfg = self.cfg

        def fail(text: str, flag: str) -> tuple[str, bool, str]:
            st.tool_errors += 1
            st.fail_streak[name] = st.fail_streak.get(name, 0) + 1
            if st.fail_streak[name] >= 2:
                text += (f"\n{_NOTE}This is failure #{st.fail_streak[name]} in a row for {name}. Do not repeat the "
                         "same call; change the arguments or approach, or tell the user what is blocking you.")
            return text, False, flag

        if c["parse_error"]:
            return fail(f"Invalid arguments for {name}: {c['parse_error']}. Nothing was run. "
                        "Call it again with a valid JSON object.", "bad_json")
        if name not in self.schemas:
            return fail(f"Unknown tool '{name}'. Nothing was run. Available tools: "
                        f"{', '.join(sorted(self.schemas))}.", "unknown_tool")
        args = coerce_args(args, self.schemas[name])
        c["args"] = args
        missing = [k for k in (self.schemas[name].get("required") or []) if args.get(k) is None]
        if missing:
            return fail(f"Missing required argument(s) for {name}: {', '.join(missing)}. Nothing was run. "
                        "Call it again with all required arguments.", "missing_args")

        if name == PLAN_TOOL_NAME:
            err = st.plan.set(args.get("steps"))
            if err:
                return fail(f"Invalid plan: {err}. Nothing was changed.", "bad_plan")
            nxt = st.plan.next_step()
            tail = (f"Next: {nxt}. Do it now by calling its tool (don't just say you will)." if nxt
                    else "All steps are finished. Give the user your final answer.")
            return f"Plan saved:\n{st.plan.render()}\n{_NOTE}{tail}", True, ""

        key = (name, json.dumps(args, sort_keys=True, default=str))
        if name in READ_ONLY_TOOLS and key in st.cache and st.cache[key][1] == st.epoch:
            st.repeats += 1
            return (f"{_NOTE}You already ran this exact call this turn; reusing its result. Don't repeat it - "
                    f"answer with it or try something different.\n{st.cache[key][0]}"), True, "repeat"

        try:
            raw = await asyncio.wait_for(self.execute(name, args), timeout=cfg.tool_timeout)
        except asyncio.TimeoutError:
            return fail(f"Tool timed out after {int(cfg.tool_timeout)}s: {name}. "
                        "Try a smaller or different request.", "timeout")
        except asyncio.CancelledError:
            raise
        except Exception as e:                              # noqa: BLE001 - a tool must never kill the turn
            logger.exception(f"Tool {name} raised")
            return fail(f"Tool failed: {name} raised {type(e).__name__}: {e}", "exception")

        result = "(no output)" if raw is None else raw if isinstance(raw, str) else json.dumps(raw, default=str)
        result = self._cap(result)
        if looks_failed(result):
            if name == "edit_local_file" and "not found in file" in result:
                result += (f"\n{_NOTE}Re-read the file with read_local_file and copy old_string exactly, "
                           "including whitespace and line breaks.")
            return fail(result, "tool_error")
        st.fail_streak[name] = 0
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
        """No tool calls this round. Decide: done / retry (messages were extended) / fallback text."""
        cfg, text = self.cfg, res.content
        if res.finish_reason == "length" and text.strip() and st.continues < cfg.max_continuations:
            st.continues += 1
            st.glue = True
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": _NOTE + "Your reply was cut off by the output limit. "
                             "Continue exactly where you stopped, without repeating anything."})
            self._trace(event="continue", n=st.continues)
            return "retry", ""
        if not text.strip():
            if st.empty_retries < cfg.max_empty_retries:
                st.empty_retries += 1
                messages.append({"role": "user", "content": _NOTE + "Your last reply was empty. " + (
                    "Use the tool results above to answer the user now, in plain text." if st.tool_calls_total
                    else "Answer the user's last message in plain text.")})
                self._trace(event="empty_reply_retry", n=st.empty_retries)
                return "retry", ""
            if st.emitted_any:
                return "done", ""
            if st.last_result:
                return "fallback", ("I ran the tools but couldn't put a proper reply together. The last result was:\n\n"
                                    + st.last_result[:800])
            return "fallback", "I didn't get a reply from the model. Please try again."
        if cfg.narration_guard and st.claim_nudges < 1 and self.tools:
            claim = unbacked_claim(text, st.ok_tools)
            if claim:
                sentence, tools = claim
                st.claim_nudges += 1
                names = "/".join(sorted(tools))
                can = "Make the tool call now, or " if not final_round else "You cannot call tools any more this turn, so "
                messages.append({"role": "assistant", "content": text})
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
            return self._broken_promise(promise, text, messages, st)
        pending = st.plan.pending()
        if pending and not final_round and st.plan.nudges < cfg.max_plan_nudges and self.tools:
            st.plan.nudges += 1
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": _NOTE + (
                "Your plan still has unfinished steps:\n" + st.plan.render() + "\n"
                f"Do the next one now: {pending[0]['text']}. Call its tool; don't just describe it. If a step is no "
                "longer needed, mark it skipped with update_plan. Only answer once every step is done or skipped.")})
            self._trace(event="plan_nudge", pending=len(pending))
            return "retry", ""
        return "done", ""

    def _broken_promise(self, promise: Promise, text: str, messages: list[dict], st: TurnState) -> tuple[str, str]:
        """The reply ended with "I'll do X" and no tool call. Escalate instead of letting the turn die:

          1st time  nudge            - tell the model to call the tool now
          2nd time  auto-act         - if it announced a web search, the harness runs it (queries from the
                                       text, else the user's last message); otherwise a firmer nudge
          3rd time  auto-act / give up honestly (never leave the user with a silent, unfulfilled promise)
        """
        cfg = self.cfg
        st.promise_streak += 1
        n = st.promise_streak
        self._trace(event="broken_promise", n=n, kind=promise.kind, sentence=promise.sentence[:140])
        can_auto = promise.kind == "search" and "web_search" in self.schemas and st.auto_acts < cfg.max_auto_acts
        if n >= 2 and can_auto:
            queries = promise.queries or [self._fallback_query(messages)]
            st.auto_calls = [{"id": f"auto_{st.rounds}_{i}", "name": "web_search", "parse_error": None,
                              "args": {"query": q}, "arguments": json.dumps({"query": q}, ensure_ascii=False)}
                             for i, q in enumerate([q for q in queries if q][:3])]
            if st.auto_calls:
                return "auto", ""
        if n <= cfg.max_promise_nudges:
            st.nudges += 1
            firm = "" if n == 1 else (" This is the second time you announced an action and then stopped. "
                                      "Your NEXT message must be the tool call itself, with no text before it.")
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": _NOTE + (
                "You said you would take an action but did not call a tool. Call the tool now - don't "
                "describe it. If no tool is needed, just answer." + firm)})
            return "retry", ""
        return "fallback", (f"I said I'd do that ({promise.verb}) but never actually ran it, so nothing happened yet. "
                            "Tell me to go ahead and I'll do it properly.")

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
