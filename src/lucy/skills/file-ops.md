---
skill_id: file-ops
name: File Operations
description: Read, write, and edit local files within the sandbox.
trigger: user asks to read, write, create, edit, or modify a file
category: filesystem
tools:
  - read_local_file
  - write_local_file
  - edit_local_file
  - list_local_files
---

# File Operations Skill

Read, write, and edit local files within the sandbox.

## Instructions

Always use write_local_file for creating files and edit_local_file formodifying existing files. Use read_local_file to inspect files before editing.
All file operations are sandboxed to C:/Users/dimay.
