# Self-evolution (P7): how Lucy improves herself without being able to break herself

Code: `src/lucy/selfdev/` (constitution: hash-locked, agent-immutable). Records: `runtime/selfdev/` (the safety layer refuses agent
writes there). Copies for Lane B: `runtime/workspaces/` (agent-writable). Tools: `user_tool` (Lane A), `core_change` (Lane B).
UI: Settings > Evolve. CLI: `rollback.bat` (= `cd src && python -m lucy.selfdev rollback|releases`).

## Lane A - Lucy's own tools (autonomous, fenced in)

```
user_tool create -> static check (AST) -> staged file -> tests in the sandbox -> files in src/lucy/tools_user/<name>.py (+ _test.py)
                 -> registry.default (hot, same turn via the live ToolCatalog) -> status "probation", tier "exec"
call             -> hash check (file == the tested one?) -> sandbox run -> result (+ probation note) -> uses / failures counted
                 -> 10 good runs: "active"; 3 failures in a row: switched off ("auto"), improve re-enables
user (Evolve)    -> tier (never below what `needs` require), switch on/off, previous version, delete
```

| Guard | Where | What it stops |
|---|---|---|
| Static check | `sandbox.static_check` | eval/exec/`__import__`, dunder tricks, ctypes/importlib/gc/inspect, network/subprocess/registry imports not in `needs`; tests need an assert |
| Child process | `sandbox.run_job` | user code never runs inside Lucy; `-I -B`, temp cwd, env without keys/tokens, time limit, output cap |
| Audit hook | `_runner.py` | writes outside temp (+ the vetted path args for `needs=files`), reading denied paths, sockets, programs, ctypes, winreg, settrace/input |
| Tier | `user_tools.json` (protected) | a new tool asks before each run (default mode) until the USER promotes it; the TIER line in the file is ignored |
| Hash | `UserTools.call` | a tool file edited outside `user_tool` is switched off, never run |
| Limits | `user_tools.py` | 12 changes/day, 40 tools, 16 k code, no built-in names, user-disabled stays off |

The hook is defence in depth, not a perfect jail. A tool with `needs=programs` can escape it through a child process; that is why
such tools are tier `exec` (asks) and why the hook is not the only fence.

## Lane B - changes to Lucy Core (gated: needs the user's click)

```
core_change start    copy live tree (no runtime/, models, caches, secrets) -> runtime/workspaces/<id>, BASE hashes recorded
(Lucy edits the copy with her normal file tools)
core_change submit   change set vs BASE; protected/constitution files -> refused at once; the set is FROZEN (hashes)
  background         1. full pytest suite in the copy   2. canary: the copy on a free port (fake LLM, own runtime, autostart off,
                     temp SAFE_ROOT)   3. smoke: health, root == copy, safety not halted, chat + tool call round trip, library, proactive
                     any red -> "failed" (live never touched)   all green -> "ready"
user: Approve        frozen set unchanged? no live conflicts? -> live versions to runtime/selfdev/releases/<rid>/before/ -> atomic writes
                     -> "applied" (restart Lucy). Write error -> automatic rollback.
user: Roll back      before/ files back, added files removed. Works with the server down (rollback.bat).
```

Never through Lane B: every file in `.core/constitution.lock`, `src/lucy/{safety,broker,selfdev,tools_user}/`, `seal.bat`, `rollback.bat`.
Policy decided with the user: **every Lane B change needs an approval click** (no auto-apply). `submit` is tier exec (asks once),
because the suite runs agent-written code unsandboxed in the copy.

## Trace mining (weekly, deterministic)

`trace_mining.py` reads the last 7 days of `runtime/logs/agent_trace.jsonl`: tool failures (normalised error text), forced calls,
tools pulled in mid-turn, tool_search misses, user denials, broken promises / unbacked claims / repeat cuts. Each pattern with
enough hits becomes an idea `{kind: lane_a|lane_b|routing, title, evidence, suggestion}` in `runtime/selfdev/ideas.json` and a note
in `runtime/data/learned/trace-YYYY-Www.md`. Nothing is changed automatically: Lucy sees them via `core_change ideas`, the user in
Settings > Evolve, and acting on one is a normal Lane A / Lane B change.

## Known gaps

- Lane B tests and the canary run agent-written code without a sandbox (in the copy, as the user). The approval card on `submit` is the gate.
- The user-only API routes are protected like the P1 ones (API key; a shell command naming `localhost:8090` always asks).
- Windows process trees: a timed-out user tool with `needs=programs` may leave its child processes running.
- Edits in `runtime/workspaces/` ask on an `ask` drive (C:). To let Lucy edit copies freely, add `"{RUNTIME}/workspaces"` to
  `folders.write` in permissions.yaml (your choice; reseal after).
