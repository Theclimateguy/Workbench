#!/usr/bin/env python3
"""MCP server `papers`: scientific literature from arXiv and Zenodo (public APIs, no keys).

Env: ZENODO_TOKEN (optional; only raises rate limits / shows your own restricted records)
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET

from mcp.server.fastmcp import FastMCP

from common import client, html_to_text, page, pdf_to_text

ATOM = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
ZENODO = "https://zenodo.org/api"
ZENODO_TOKEN = os.environ.get("ZENODO_TOKEN", "")
mcp = FastMCP("papers")


def _arxiv_id(s: str) -> str:
    s = s.strip()
    m = re.search(r"(\d{4}\.\d{4,5}(v\d+)?|[a-z\-]+(\.[A-Z]{2})?/\d{7}(v\d+)?)", s)
    return m.group(1) if m else s


def _entry(e: ET.Element, full: bool) -> str:
    g = lambda tag: (e.findtext(tag, "", ATOM) or "").strip()
    aid = g("a:id").rsplit("/abs/", 1)[-1]
    authors = [a.findtext("a:name", "", ATOM) for a in e.findall("a:author", ATOM)]
    cats = [c.get("term") for c in e.findall("a:category", ATOM)]
    head = f"[{aid}] {' '.join(g('a:title').split())}\n  {', '.join(authors[:6])}{' et al.' if len(authors) > 6 else ''}" \
           f" · {g('a:published')[:10]} · {', '.join(cats[:4])}\n  https://arxiv.org/abs/{aid}"
    doi = g("arxiv:doi")
    if doi:
        head += f" · doi:{doi}"
    summary = " ".join(g("a:summary").split())
    return head + "\n  " + (summary if full else summary[:350] + ("…" if len(summary) > 350 else ""))


@mcp.tool()
def arxiv_search(query: str, max_results: int = 10, sort: str = "relevance", category: str = "") -> str:
    """Search arXiv. query is free text; arXiv field syntax also works (ti:, au:, abs:, AND/OR).
    sort: 'relevance' or 'date' (newest first). category: e.g. 'cs.LG', 'physics.ao-ph' (optional)."""
    q = query if re.search(r"\b(ti|au|abs|all|cat):", query) else " AND ".join(f"all:{w}" for w in query.split())
    if category:
        q = f"({q}) AND cat:{category}"
    params = {"search_query": q, "start": 0, "max_results": max(1, min(max_results, 30)),
              "sortBy": "submittedDate" if sort == "date" else "relevance", "sortOrder": "descending"}
    with client() as c:
        r = c.get("https://export.arxiv.org/api/query", params=params)
        r.raise_for_status()
    entries = ET.fromstring(r.text).findall("a:entry", ATOM)
    return "\n\n".join(_entry(e, False) for e in entries) or "No results."


@mcp.tool()
def arxiv_paper(arxiv_id: str, full_text: bool = False, offset: int = 0) -> str:
    """Get one arXiv paper: metadata and abstract; with full_text=true also the paper text
    (HTML version if arXiv has one, otherwise the PDF). Paged: pass offset to continue."""
    aid = _arxiv_id(arxiv_id)
    with client() as c:
        r = c.get("https://export.arxiv.org/api/query", params={"id_list": aid})
        r.raise_for_status()
        entries = ET.fromstring(r.text).findall("a:entry", ATOM)
        if not entries:
            return f"arXiv id {aid} not found."
        meta = _entry(entries[0], True)
        if not full_text:
            return meta
        h = c.get(f"https://arxiv.org/html/{aid}")
        if h.status_code == 200 and "<html" in h.text[:2000].lower():
            text = html_to_text(h.text, str(h.url))
        else:
            p = c.get(f"https://arxiv.org/pdf/{aid}")
            p.raise_for_status()
            text = pdf_to_text(p.content)
    return (meta + "\n\n--- full text ---\n" if offset == 0 else "") + page(text, offset)


def _zget(path: str, **params):
    headers = {"Authorization": f"Bearer {ZENODO_TOKEN}"} if ZENODO_TOKEN else {}
    with client(headers=headers) as c:
        r = c.get(f"{ZENODO}{path}", params=params)
        r.raise_for_status()
        return r.json()


def _zrecord(h: dict, full: bool) -> str:
    m = h.get("metadata", {})
    creators = [c.get("name", "") for c in m.get("creators", [])]
    head = f"[zenodo:{h.get('id')}] {m.get('title', '').strip()}\n  {'; '.join(creators[:6])}{' et al.' if len(creators) > 6 else ''}" \
           f" · {m.get('publication_date', '')} · {m.get('resource_type', {}).get('title', '')}" \
           f"\n  https://zenodo.org/records/{h.get('id')}" + (f" · doi:{h['doi']}" if h.get("doi") else "")
    desc = html_to_text(m.get("description", "") or "")
    if not full:
        return head + "\n  " + " ".join(desc.split())[:350]
    out = [head, "", desc]
    if m.get("keywords"):
        out.append("\nKeywords: " + ", ".join(m["keywords"]))
    if m.get("license"):
        out.append("License: " + (m["license"].get("id", "") if isinstance(m["license"], dict) else str(m["license"])))
    files = h.get("files") or []
    if files:
        out.append("\nFiles (read with fetch_url):")
        for f in files:
            link = f.get("links", {}).get("self", "")
            out.append(f"  - {f.get('key')} ({f.get('size', 0) / 1e6:.1f} MB) {link}")
    return "\n".join(out)


@mcp.tool()
def zenodo_search(query: str, max_results: int = 10, resource_type: str = "", sort: str = "bestmatch") -> str:
    """Search Zenodo (datasets, software, papers, posters, …). Elasticsearch syntax works
    (e.g. title:"climate model" AND creators.name:Smith). resource_type: dataset, software,
    publication, poster, presentation, image, video (optional). sort: 'bestmatch' or 'newest'."""
    params = {"q": query, "size": max(1, min(max_results, 30)), "sort": "newest" if sort == "newest" else "bestmatch"}
    if resource_type:
        params["type"] = resource_type
    hits = _zget("/records", **params).get("hits", {}).get("hits", [])
    return "\n\n".join(_zrecord(h, False) for h in hits) or "No results."


@mcp.tool()
def zenodo_record(record_id: str) -> str:
    """Get one Zenodo record: full description, keywords, license and the list of files with download links.
    record_id: number, 'zenodo:123', a zenodo.org URL or a 10.5281/zenodo.N DOI."""
    m = re.search(r"(\d{4,})\s*$", record_id.strip().rstrip("/"))
    if not m:
        return "Could not find a record number in " + record_id
    return _zrecord(_zget(f"/records/{m.group(1)}"), True)


if __name__ == "__main__":
    mcp.run()
