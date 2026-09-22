---
name: web-search-extract
description: Search the web and extract readable content from URLs. Uses web_search and web_extract tools.
triggers: user asks to search the web, look up information online, read a URL, extract content from a webpage, research a topic, look up info
category: web
---

# Web Search & Extract

Uses `web_search` and `web_extract` tools for live web access.

## Workflow

1. **Search ONCE** with `web_search(query: str, limit: int=5)` — returns top 5 results with titles, URLs, and snippets.
2. **Pick the best result** from the search output — look at the title and snippet to find the most relevant URL.
3. **Extract content** with `web_extract(urls: list[str], char_limit: int=15000)` on ONLY the single most relevant URL.
4. **Synthesize** a concise answer from the extracted content. Do NOT search again.

## Critical Rules

- **Search once per query. Never search twice.** After web_search returns results, immediately call web_extract on the best URL.
- **Only extract from ONE URL** — the most relevant one. Never extract from all 5.
- **If search results look wrong** (e.g., about the wrong topic), adjust the query and search ONCE more. But do not spam searches.
- **Always cite sources** with URLs when referencing web content.
- The snippets in search results already contain useful info. If a snippet fully answers the question, summarize directly without extracting.

## Examples

```
web_search(query="health benefits of mandarin oranges", limit=5)
web_extract(urls=["https://en.wikipedia.org/wiki/Mandarin_orange"], char_limit=15000)
```
