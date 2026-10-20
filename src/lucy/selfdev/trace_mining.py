"""Weekly trace mining: read runtime/logs/agent_trace.jsonl, find failure patterns, write concrete improvement ideas.

Deterministic (no model call), so it works the same with a 4B or a 12B model. Ideas are SUGGESTIONS: each says which
lane fits ("lane_a" = Lucy can write a tool for it, "lane_b" = a fix in Lucy Core, "routing" = keyword tuning, which is
also a Lane B change). Nothing is changed automatically: the user (Settings > Evolve) or Lucy (`core_change ideas`)
picks one up, and a Lane B change still needs every check + the user's approval click.
Output: runtime/selfdev/ideas.json (+ a note in runtime/data/learned/trace-YYYY-Www.md).
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Dict, List, Optional

WEEK = 7 * 86400
MIN_COUNT = 3                       # a pattern needs this many hits in the window to become an idea
MAX_IDEAS = 12
READ_BYTES = 8_000_000


def _norm_error(e: str) -> str:
    e = re.sub(r"[A-Za-z]:[\\/][^\s'\"]+|/[^\s'\"]+/[^\s'\"]+", "<path>", str(e or ""))
    e = re.sub(r"\d+", "N", e)
    return " ".join(e.split())[:90]


def read_events(path: Path, since_ts: float) -> List[dict]:
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since_ts))
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - READ_BYTES))
            lines = f.read().decode("utf-8", "ignore").splitlines()
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if isinstance(e, dict) and str(e.get("ts", "")) >= since:
            out.append(e)
    return out


def analyse(events: List[dict]) -> List[dict]:
    """Patterns -> ideas, most evidence first."""
    fails: Dict[str, Counter] = defaultdict(Counter)
    calls: Counter = Counter()
    forced: Counter = Counter()
    searches_missed: Counter = Counter()
    auto_added: Counter = Counter()
    denied: Counter = Counter()
    counts: Counter = Counter()
    promises: List[str] = []
    for e in events:
        ev = e.get("event")
        if ev == "tool":
            t = str(e.get("tool"))
            calls[t] += 1
            if e.get("ok") is False:
                fails[t][_norm_error(e.get("error"))] += 1
        elif ev == "forced_call":
            forced[str(e.get("tool"))] += 1
        elif ev == "tool_search":
            if not e.get("found"):
                searches_missed[" ".join(str(e.get("query", "")).lower().split())[:60]] += 1
        elif ev == "tool_auto_added":
            auto_added[str(e.get("tool"))] += 1
        elif ev == "tool_denied":
            denied[str(e.get("tool"))] += 1
        elif ev in ("broken_promise", "unbacked_claim", "repeat_cut", "truncated_tool_call", "empty_reply_retry"):
            counts[ev] += 1
            if ev == "broken_promise" and e.get("sentence"):
                promises.append(str(e["sentence"])[:100])
    ideas: List[dict] = []

    def add(kind, title, evidence, suggestion, weight):
        ideas.append({"kind": kind, "title": title, "evidence": evidence, "suggestion": suggestion, "weight": weight})

    for tool, errs in fails.items():
        n = sum(errs.values())
        if n < MIN_COUNT:
            continue
        top, top_n = errs.most_common(1)[0]
        rate = n / max(1, calls[tool])
        if any(w in top.lower() for w in ("missing", "required", "unexpected keyword", "invalid", "must be", "expected")):
            sug = (f"The model often calls {tool} with wrong arguments. Make the schema harder to misuse in "
                   f"src/lucy/tools/ (clearer parameter descriptions, accept the common mistake as an alias).")
        elif "timed out" in top.lower() or "timeout" in top.lower():
            sug = f"{tool} keeps timing out: raise its default timeout or run it as a background job."
        elif "not found" in top.lower() or "no such file" in top.lower() or "did you mean" in top.lower():
            sug = f"{tool} keeps missing paths: improve the 'did you mean' hint or resolve the path the model means."
        else:
            sug = f"Look at why {tool} fails and fix the cause (or make the error message tell the model how to recover)."
        add("lane_b", f"{tool} failed {n}x ({rate:.0%} of its calls)",
            f"most common error ({top_n}x): {top}", sug, n * 2)
    for tool, n in forced.items():
        if n >= MIN_COUNT:
            add("lane_b", f"the harness had to force {tool} {n}x",
                "the model announced the action but did not emit the call",
                f"Make {tool} easier to call: shorter description, fewer required arguments, or add its trigger words to "
                "routing.FAMILIES so it is in the prompt from the start.", n * 2)
    for tool, n in auto_added.items():
        if n >= MIN_COUNT:
            add("routing", f"{tool} was pulled in mid-turn {n}x",
                "it was not in the prompt but the model needed it",
                f"Add the user's words for this to the family of {tool} in src/lucy/tools/routing.py (FAMILIES).", n)
    for q, n in searches_missed.items():
        if n >= 2:
            add("lane_a", f"tool_search found nothing for '{q}' ({n}x)",
                "the model looked for a tool that does not exist",
                f"Write a user tool for '{q}' (user_tool action=create) if it is a pure calculation / text job; otherwise "
                "add keywords so an existing tool is found.", n * 3)
    for tool, n in denied.items():
        if n >= MIN_COUNT:
            add("lane_b", f"the user denied {tool} {n}x",
                "Lucy keeps asking for something the user does not want",
                f"Teach Lucy (harness.md / persona SELF) when NOT to use {tool}, or offer a safer alternative first.", n)
    labels = {"broken_promise": "said she would act and did not", "unbacked_claim": "claimed an action she had not done",
              "repeat_cut": "got stuck repeating herself", "truncated_tool_call": "tool call cut off (too long)",
              "empty_reply_retry": "empty replies"}
    for ev, n in counts.items():
        if n >= MIN_COUNT:
            ex = f" e.g. \"{promises[-1]}\"" if ev == "broken_promise" and promises else ""
            add("lane_b", f"{labels[ev]}: {n}x", f"{n} times this week{ex}",
                "Check agent_trace.jsonl around these turns and tighten the matching nudge in agent_loop.py / multistep.py "
                "(add the phrasing as a test case first).", n)
    ideas.sort(key=lambda i: -i["weight"])
    for k, i in enumerate(ideas[:MAX_IDEAS], 1):
        i["n"] = k
    return ideas[:MAX_IDEAS]


class TraceMiner:
    def __init__(self, *, trace_path: Path, state_dir: Path, notes_dir: Optional[Path] = None,
                 clock: Callable[[], float] = time.time):
        self.trace_path, self.state_dir = Path(trace_path), Path(state_dir)
        self.ideas_path = self.state_dir / "ideas.json"
        self.notes_dir = Path(notes_dir) if notes_dir else None
        self.clock = clock

    def load(self) -> dict:
        try:
            return json.loads(self.ideas_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"ran": 0, "ideas": [], "events": 0}

    def due(self) -> bool:
        return self.clock() - float(self.load().get("ran") or 0) >= WEEK

    def run(self, reason: str = "weekly") -> dict:
        now = self.clock()
        events = read_events(self.trace_path, now - WEEK)
        res = {"ran": now, "reason": reason, "events": len(events), "ideas": analyse(events),
               "window": time.strftime("%Y-%m-%d", time.localtime(now - WEEK)) + " .. "
                         + time.strftime("%Y-%m-%d", time.localtime(now))}
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.ideas_path.with_name("ideas.json.tmp")
        tmp.write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.ideas_path)
        if self.notes_dir:
            try:
                self.notes_dir.mkdir(parents=True, exist_ok=True)
                wk = time.strftime("%Y-W%W", time.localtime(now))
                lines = [f"# Trace review {res['window']} ({res['events']} events)", ""]
                lines += [f"{i['n']}. [{i['kind']}] {i['title']} - {i['suggestion']}" for i in res["ideas"]] or ["Nothing stood out."]
                (self.notes_dir / f"trace-{wk}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
            except OSError:
                pass
        return res

    def text(self, limit: int = 6) -> str:
        d = self.load()
        if not d.get("ran"):
            return "No trace review yet (it runs weekly; the user can start one in Settings > Evolve)."
        ideas = d.get("ideas") or []
        if not ideas:
            return f"Trace review {d.get('window')}: nothing stood out ({d.get('events')} events)."
        head = f"Trace review {d.get('window')} ({d.get('events')} events). Ideas, best first:"
        return head + "\n" + "\n".join(f"{i['n']}. [{i['kind']}] {i['title']}: {i['suggestion']} ({i['evidence']})"
                                       for i in ideas[:limit])
