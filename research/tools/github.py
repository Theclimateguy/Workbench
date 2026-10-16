#!/usr/bin/env python3
"""MCP server `github`: read-only GitHub access (search repos/code/issues, read files and READMEs).

Env: GITHUB_TOKEN (optional; required for code search and private repos; a fine-grained read-only token is enough)
"""
from __future__ import annotations

import base64
import os

from mcp.server.fastmcp import FastMCP

from common import client, page

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
mcp = FastMCP("github")


def _get(path: str, raw: bool = False, **params):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    with client(headers=headers) as c:
        r = c.get(f"{API}{path}", params=params)
    if r.status_code == 404:
        raise ValueError(f"not found: {path}")
    if r.status_code in (401, 403):
        raise ValueError(f"GitHub HTTP {r.status_code}: {r.json().get('message', '')}"
                         + ("" if TOKEN else " (no GITHUB_TOKEN configured)"))
    r.raise_for_status()
    return r.json()


@mcp.tool()
def gh_search_repos(query: str, max_results: int = 10, sort: str = "best") -> str:
    """Search GitHub repositories. Qualifiers work: language:python stars:>100 topic:climate user:NAME.
    sort: 'best', 'stars' or 'updated'."""
    params = {"q": query, "per_page": max(1, min(max_results, 30))}
    if sort in ("stars", "updated"):
        params["sort"] = sort
    items = _get("/search/repositories", **params).get("items", [])
    return "\n\n".join(
        f"{r['full_name']} ★{r['stargazers_count']} · {r.get('language') or '-'} · updated {r['pushed_at'][:10]}\n"
        f"  {r['html_url']}\n  {(r.get('description') or '').strip()[:300]}" for r in items) or "No results."


@mcp.tool()
def gh_search_code(query: str, max_results: int = 10) -> str:
    """Search code on GitHub (needs GITHUB_TOKEN). Qualifiers: repo:OWNER/NAME language:X path:dir extension:py."""
    items = _get("/search/code", q=query, per_page=max(1, min(max_results, 30))).get("items", [])
    return "\n".join(f"{i['repository']['full_name']}: {i['path']}  {i['html_url']}" for i in items) or "No results."


@mcp.tool()
def gh_search_issues(query: str, max_results: int = 10) -> str:
    """Search issues and pull requests. Qualifiers: repo:OWNER/NAME is:issue|is:pr is:open label:bug."""
    items = _get("/search/issues", q=query, per_page=max(1, min(max_results, 30))).get("items", [])
    return "\n\n".join(
        f"{'PR' if 'pull_request' in i else 'Issue'} #{i['number']} [{i['state']}] {i['title']}\n  {i['html_url']}\n"
        f"  {(i.get('body') or '').strip()[:300]}" for i in items) or "No results."


@mcp.tool()
def gh_repo(repo: str) -> str:
    """Overview of a repository (OWNER/NAME): description, stats, topics, license and its README."""
    r = _get(f"/repos/{repo}")
    head = (f"{r['full_name']} ★{r['stargazers_count']} forks {r['forks_count']} · {r.get('language') or '-'}"
            f" · default branch {r['default_branch']} · updated {r['pushed_at'][:10]}\n{r['html_url']}\n"
            f"{r.get('description') or ''}\nTopics: {', '.join(r.get('topics', []))}"
            f"\nLicense: {(r.get('license') or {}).get('spdx_id', '-')}")
    try:
        readme = base64.b64decode(_get(f"/repos/{repo}/readme")["content"]).decode("utf-8", "replace")
    except ValueError:
        readme = "(no README)"
    return head + "\n\n--- README ---\n" + page(readme, 0, 8000)


@mcp.tool()
def gh_read(repo: str, path: str = "", ref: str = "", offset: int = 0) -> str:
    """Read a file or list a directory in a repository (OWNER/NAME). path '' = repository root.
    ref: branch, tag or commit (optional). Long files are paged: pass offset to continue."""
    params = {"ref": ref} if ref else {}
    data = _get(f"/repos/{repo}/contents/{path.strip('/')}", **params)
    if isinstance(data, list):
        return "\n".join(f"{'dir ' if e['type'] == 'dir' else 'file'} {e['path']}" + (f"  ({e['size']} B)" if e['type'] == 'file' else "")
                         for e in sorted(data, key=lambda e: (e["type"] != "dir", e["path"])))
    if data.get("encoding") == "base64":
        text = base64.b64decode(data["content"]).decode("utf-8", "replace")
    else:  # files > 1 MB come without content
        with client() as c:
            text = c.get(data["download_url"]).text
    return page(text, offset)


if __name__ == "__main__":
    mcp.run()
