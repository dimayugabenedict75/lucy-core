---
name: Tool Timeout Recovery
description: Recover from tool timeouts by narrowing scope and using alternatives.
triggers: timeout timed out slow search
category: debugging
---

## Instructions

When a tool times out:
1. **Narrow search scope** — use smaller directory paths with maxdepth flag
2. **Use more specific patterns** — add context to your query
3. **Switch tools** — if grep_search is slow, try find_file first, then read specific files
4. **Check path exists first** — verify the directory before searching
5. **Break large tasks** — split into smaller chunks

## Context

This skill triggers on common timeout keywords and helps the agent recover
from hanging subprocess calls or slow filesystem operations.