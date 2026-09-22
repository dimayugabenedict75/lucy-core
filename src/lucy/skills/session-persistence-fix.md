---
skill_id: session-persistence-fix
name: session-persistence-fix
description: Diagnose and fix session and memory persistence failures.
trigger: memory not persisting session lost conversations reset
category: devops
tools: [
  "read_local_file",
  "run_shell_command",
]
---

When memory not persisting or endpoints returning unexpected results:

1. **Kill stale server processes first — check what's actually on the port.** `netstat -a -n -o | grep 8086 | findstr LISTENING` shows the PID. The process you think is dead may still be alive — a new `uvicorn` spawn fails silently or binds a different PID while the old one holds the port. Use `taskkill /F /PID <pid> /T` on the LISTENING PID, and repeat until the port is free.

2. **Clear Python bytecode cache.** Old `__pycache__/*.pyc` files cause the server to run stale code even after patches. Run `find . -name __pycache__ -exec rm -rf {} +; find . -name '*.pyc' -delete` before restarting.

3. Check sqlite db files exist. 4. Verify file permissions. 5. Check for stale WAL files. 6. Inspect server startup logs. 7. Verify seed_defaults ran. 8. Restart server if corrupted.

**Pitfall:** A 404 or `AttributeError: 'bool' object has no attribute 'append'` that persists after fixing the code is almost always a stale server process with old bytecode — not a code bug. Always confirm the PID on the port before debugging logic.
