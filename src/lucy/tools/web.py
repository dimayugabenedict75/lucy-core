"""Web tools (search + page extract). Moved out of api.py with the code kept as-is."""
import httpx


async def _web_search(query: str, limit: int = 5) -> str:
    """Search Bing and return top results as formatted text."""
    import re
    import html as html_module
    import base64
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(
                "https://www.bing.com/search",
                params={"q": query, "count": limit, "setlang": "en-US"},
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.5",
                },
            )
            if resp.status_code != 200:
                return f"Search failed with status {resp.status_code}"
            html_text = resp.text
    except Exception as e:
        return f"Search error: {e}"

    results = []
    links = re.findall(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html_text, re.DOTALL)
    cites = re.findall(r'<cite[^>]*>(.*?)</cite>', html_text, re.DOTALL)
    snippets = re.findall(r'class="b_caption"[^>]*>(.*?)</div>', html_text, re.DOTALL)

    for i in range(min(limit, len(links))):
        href, title_raw = links[i]
        title = html_module.unescape(re.sub(r'<[^>]+>', '', title_raw).strip())
        result_url = ""
        # Try to decode Bing redirect URL
        match = re.search(r'u=a1([A-Za-z0-9+/=]+)', href)
        if match:
            try:
                decoded = base64.b64decode(match.group(1)).decode('utf-8', errors='replace')
                result_url = decoded
            except Exception:
                pass
        # Fall back to cite for URL
        if not result_url and i < len(cites):
            cite_text = html_module.unescape(re.sub(r'<[^>]+>', '', cites[i]).strip())
            url_match = re.search(r'https?://[^\s]+', cite_text)
            if url_match:
                result_url = url_match.group(0).replace('&nbsp;', '').replace(' › ', '/').strip()
        # Also try extracting URL from the href itself if it's a direct link
        if not result_url:
            direct_url = re.search(r'https?://[^\s&]+', href)
            if direct_url:
                result_url = direct_url.group(0)
        snippet = ""
        if i < len(snippets):
            snippet = html_module.unescape(re.sub(r'<[^>]+>', '', snippets[i]).strip())
            snippet = snippet.replace('\n', ' ')[:200]
        results.append(f"[{i+1}] {title}\n    URL: {result_url}\n    {snippet}")

    if not results:
        return f"No results found for '{query}'"
    return "\n\n".join(results)


async def _web_extract(urls: list, char_limit: int = 15000) -> str:
    """Extract clean content from web pages using Jina Reader API, with fallback to direct scraping."""
    if isinstance(urls, str):
        urls = [urls]
    results = []
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            for url in urls:
                jina_url = f"https://r.jina.ai/{url}"
                try:
                    resp = await client.get(jina_url, timeout=30.0)
                    if resp.status_code == 200:
                        content = resp.text
                        if len(content) > char_limit:
                            content = content[:char_limit] + "\n\n[... truncated, full page was longer ...]"
                        results.append(f"=== {url} ===\n{content}")
                    else:
                        # Fallback: try Wikipedia API for Wikipedia URLs
                        if "wikipedia.org" in url:
                            api_url = url.replace("/wiki/", "/w/api.php").replace("/w/api.php", "/w/api.php")
                            # Better: use the REST API summary endpoint
                            title = url.split("/wiki/")[-1] if "/wiki/" in url else ""
                            if title:
                                rest_url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
                                resp2 = await client.get(rest_url, headers={"User-Agent": "LucyCore/1.0"})
                                if resp2.status_code == 200:
                                    import json
                                    data = json.loads(resp2.text)
                                    extract = data.get("extract", "")
                                    content = f"# {data.get('title', title)}\n\n{extract}"
                                    if len(content) > char_limit:
                                        content = content[:char_limit] + "\n\n[... truncated ...]"
                                    results.append(f"=== {url} ===\n{content}")
                                else:
                                    results.append(f"=== {url} ===\n[Failed to extract Wikipedia page: status {resp2.status_code}]")
                            else:
                                results.append(f"=== {url} ===\n[Could not parse Wikipedia URL]")
                        else:
                            results.append(f"=== {url} ===\n[Failed to extract — Jina status {resp.status_code}]")
                except Exception as e:
                    results.append(f"=== {url} ===\n[Error extracting: {e}]")
    except Exception as e:
        return f"Extraction error: {e}"

    return "\n\n".join(results) if results else "No content extracted"


_SCHEMA_WEB_SEARCH = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for information. Returns top results with titles, URLs, and snippets. Use this to discover current information, research topics, find articles, and gather background context.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query (e.g. 'health benefits of mandarin oranges')"
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of results to return (default: 5, max: 10)",
                    "default": 5
                }
            },
            "required": ["query"]
        }
    }
}

async def _h_web_search(args, ctx):
    query = args.get("query", "")
    limit = args.get("limit", 5)
    return await _web_search(query, limit)


_SCHEMA_WEB_EXTRACT = {
    "type": "function",
    "function": {
        "name": "web_extract",
        "description": "Extract clean, readable text content from one or more web pages. Converts HTML to markdown. Use after web_search to read the full content of the most relevant result.",
        "parameters": {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of URLs to extract content from. Only extract from the most relevant 1-2 URLs, never all 5 search results."
                },
                "char_limit": {
                    "type": "integer",
                    "description": "Maximum characters to return per page (default: 15000). Full text is saved to file for large pages.",
                    "default": 15000
                }
            },
            "required": ["urls"]
        }
    }
}

async def _h_web_extract(args, ctx):
    urls = args.get("urls", [])
    char_limit = args.get("char_limit", 15000)
    return await _web_extract(urls, char_limit)


TOOLS = [
    {"schema": _SCHEMA_WEB_SEARCH, "handler": _h_web_search, "tier": "network", "label": "search"},
    {"schema": _SCHEMA_WEB_EXTRACT, "handler": _h_web_extract, "tier": "network", "label": "read"},
]
