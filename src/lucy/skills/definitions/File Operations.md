---
name: File Operations
description: Read, write, and edit local files within the sandbox.
triggers: user asks to read, write, create, edit, or modify a file
category: filesystem
---

# File Operations Skill

Read, write, and edit local files within the sandbox.

## Instructions

Always use write_local_file for creating files and edit_local_file formodifying existing files. Use read_local_file to inspect files before editing.
All file operations are sandboxed to C:/Users/dimay.