---
skill_id: search
name: File Search
description: Search for files and content on the local filesystem.
trigger: user asks to find a file, search contents, locate something
category: search
tools:
  - find_file
  - grep_search
---

# File Search Skill

Search for files and content on the local filesystem.

## Instructions

Use find_file for locating files (bounded maxdepth 4).
Use grep_search for content searches within files.
Both have a 10s timeout to prevent server lockup.
