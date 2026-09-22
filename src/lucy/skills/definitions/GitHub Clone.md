---
name: GitHub Clone
description: Clone GitHub repositories to the local filesystem using git
triggers: github clone repository repo git
category: dev
---

# GitHub Clone

Use the `github_clone` tool when the user asks to clone a GitHub repository.

## Usage Patterns

- `github_clone(repo_url="https://github.com/NousResearch/Hermes")` — clone to default path
- `github_clone(repo_url="https://github.com/user/repo", dest="C:/Users/dimay/workspace/repo")` — clone to custom path

## Notes

- Default destination: `C:/Users/dimay/workspace/<repo-name>`
- Requires git to be installed (Git for Windows)
- Private repos require prior authentication (`gh auth login`)
- Cloned repos are readable via `read_local_file`, `list_local_files`, etc.