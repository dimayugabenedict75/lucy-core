---
skill_id: debugging-approach
name: debugging-approach
description: A structured approach to handling tool failures and errors.
trigger: debugging
category: process
tools: []
---

When a tool fails or an error occurs, follow these steps:
1. Check the error message: Identify exactly what went wrong (e.g., 'File not found', 'Connection timed out').
2. **Verify the runtime state before the code.** A 500 or 404 on a running server often means a stale process is serving old bytecode — check `netstat` for the actual PID on the port and compare to what you expect. Kill stale processes with `taskkill /F /PID <pid> /T` and clear `__pycache__` before concluding the code is wrong.
3. Verify inputs: Ensure all parameters passed to the tool are correct and complete.
4. Try alternatives: Attempt different ways to achieve the same result (e.g., different search terms, alternative paths).
5. Document results: Record what worked and what didn't for future reference.
