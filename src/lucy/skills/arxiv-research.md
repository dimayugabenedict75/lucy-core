---
name: arxiv-research
description: "Search and read arXiv papers by keyword, author, category, or ID. Includes Semantic Scholar for citations and recommendations."
version: 1.0.0
author: Lucy Harness
platforms: [linux, macos, windows]
trigger: user asks to search arxiv, find academic papers, read research, look up citations, find related work
category: research
tools:
  - web_search
  - web_extract
  - run_shell_command
---

# arXiv Research

Search and retrieve academic papers from arXiv via their free REST API + Semantic Scholar for citations.

## Quick Reference

```bash
# Search arXiv papers (via run_shell_command)
curl -s "https://export.arxiv.org/api/query?search_query=all:QUERY&max_results=5"

# Get paper metadata + citations (Semantic Scholar)
curl -s "https://api.semanticscholar.org/graph/v1/paper/arXiv:ID?fields=title,authors,citationCount,abstract"
```

## Search Query Syntax

| Prefix | Searches | Example |
|--------|----------|---------|
| `all:` | All fields | `all:transformer attention` |
| `ti:` | Title | `ti:large language models` |
| `au:` | Author | `au:vaswani` |
| `abs:` | Abstract | `abs:reinforcement learning` |
| `cat:` | Category | `cat:cs.AI` |

## Complete Workflow

1. **Discover**: `web_search(query="topic site:arxiv.org")`
2. **Assess impact**: curl Semantic Scholar API for citationCount
3. **Read abstract**: `web_extract(urls=["https://arxiv.org/abs/ID"])`
4. **Read full paper**: `web_extract(urls=["https://arxiv.org/pdf/ID"])`
5. **Find related work**: Semantic Scholar references endpoint
6. **Get recommendations**: Semantic Scholar recommendations endpoint

## Rate Limits

| API | Rate | Auth |
|-----|------|------|
| arXiv | ~1 req / 3 seconds | None needed |
| Semantic Scholar | 1 req / second | None (keyed for higher) |

## Notes

- arXiv returns Atom XML — use helper script or parse for clean output.
- Semantic Scholar returns JSON — pipe through `python3 -m json.tool` for readability.
- arXiv IDs: `1706.03762` (latest) vs `1706.03762v1` (specific version).
