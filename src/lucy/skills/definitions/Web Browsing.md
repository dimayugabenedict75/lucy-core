---
name: Web Browsing
description: Fetch, parse, and browse web pages for research and information gathering.
triggers: user asks to search the web, look up information, read a URL, browse a page, what is, who is, latest
category: web
---

# Web Browsing Skill

## Purpose
Information gathering from the web — searching, reading pages, extracting content.

## Instructions
- Use `web_search(query, limit=5)` to find relevant URLs on a topic. Search ONCE per question — do not search multiple times unless the first results are completely irrelevant.
- Use `web_extract(urls, char_limit=15000)` to extract clean markdown content from web pages. Only extract from the MOST relevant search result, never all 5.
- Always cite sources with their URLs when referencing web content.
- For pages over 15KB, `web_extract` returns head + tail sections by default — the full text is saved to a file (path included in the result footer) so you can page through it.
- Combine search + extract: search for a topic, then extract the most relevant result URLs. If search results already contain a snippet that answers the question, synthesize directly — no need to extract.
- If a page fails (403/429/timeout), note that to the user and try the next result.
- Works for: any publicly accessible URL. Fails for: sites requiring login, heavily bot-walled pages.
- Primary search provider: DuckDuckGo HTML (https://html.duckduckgo.com/html/) — reliable, not geo-targeted.
- Fallback search provider: Bing HTML with en-US locale.
- Extraction uses Jina Reader API (https://r.jina.ai/) — converts any web page to clean markdown.