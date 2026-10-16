#!/usr/bin/env python3
"""MCP server `web`: internet search through a local SearXNG (no API keys, no account) and page reading.

Env: SEARXNG_URL (default http://127.0.0.1:8888)
"""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from common import client, get, page, response_to_text

SEARXNG = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888").rstrip("/")
mcp = FastMCP("web")


@mcp.tool()
def web_search(query: str, max_results: int = 8, time_range: str = "") -> str:
    """Search the internet. Returns titles, URLs and snippets.
    time_range: '' (any), 'day', 'month' or 'year'. Use fetch_url to read a result in full."""
    params = {"q": query, "format": "json"}
    if time_range in ("day", "month", "year"):
        params["time_range"] = time_range
    with client() as c:
        r = get(c, f"{SEARXNG}/search", params=params)
        r.raise_for_status()
        data = r.json()
    out = []
    for i, res in enumerate(data.get("results", [])[:max(1, min(max_results, 20))], 1):
        date = f" ({res['publishedDate'][:10]})" if res.get("publishedDate") else ""
        out.append(f"{i}. {res.get('title', '').strip()}{date}\n   {res.get('url')}\n   {(res.get('content') or '').strip()[:400]}")
    if data.get("answers"):
        out.insert(0, "Instant answer: " + "; ".join(str(a.get("answer", a)) if isinstance(a, dict) else str(a) for a in data["answers"]))
    return "\n".join(out) or "No results."


@mcp.tool()
def fetch_url(url: str, offset: int = 0) -> str:
    """Download a web page or PDF and return its readable text (paged: pass offset to continue)."""
    if not url.startswith(("http://", "https://")):
        return "Only http(s) URLs are supported."
    with client() as c:
        r = get(c, url)
    if r.status_code >= 400:
        return f"HTTP {r.status_code} for {url}"
    return f"URL: {r.url}\n\n" + page(response_to_text(r), offset)


if __name__ == "__main__":
    mcp.run()
