---
skill_id: shell
name: shell-operations
description: Execute shell commands for system inspection.
trigger: user asks to run a command, check processes, inspect system state
category: system
tools:
  - run_shell_command
---

# Shell Operations Skill

Execute shell commands for system inspection.

## Instructions

Use run_shell_command for system inspection only. Timeout is 10s.
Do not use for writing files — use write_local_file instead.
