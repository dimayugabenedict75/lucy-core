---
name: web-search-extract
description: "Search the web and extract readable content from URLs. Uses Hermes' web_search and web_extract tools."
version: 1.0.0
author: Lucy Harness
platforms: [linux, macos, windows]
trigger: user asks to search the web, look up information online, read a URL, extract content from a webpage
category: web
tools:
  - web_search
  - web_extract
---

# Web Search & Extract

Uses Hermes' native `web_search` and `web_extract` tools for live web access.

## Workflow

1. **Search first** with `web_search(query: str, limit: int=5)` — returns top results with titles, URLs, and snippets.
2. **Extract content** with `web_extract(urls: list[str], char_limit: int=None)` — returns clean markdown from one or more URLs.

## Examples

```
web_search(query="latest llama.cpp Vulkan performance 2025", limit=5)
web_extract(urls=["https://github.com/ggml-org/llama.cpp"], char_limit=15000)
```

## Best Practices

- Search before extracting — find the right URLs first.
- Use `char_limit` to cap response size; full text is saved to a file for large pages.
- `web_extract` handles HTML, PDF (via Jina), and most web formats.
- For academic papers, combine with the `arxiv` workflow: search arXiv → extract the abstract or PDF.
