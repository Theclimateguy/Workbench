#!/usr/bin/env python3
"""MCP server `library`: read-only access to the offline document library (library_core.py).

Tools (all class "read"): library_search, library_read, library_list. Used by the Workbench agent (opencode.json)
and by the research chat. Env: LIBRARY_DIR (default /opt/library).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mcp.server.fastmcp import FastMCP  # noqa: E402

import library_core as lib  # noqa: E402

mcp = FastMCP("library")


@mcp.tool()
def library_search(query: str, k: int = 8, kind: str = "") -> str:
    """Search the offline library: equipment manuals, datasheets, troubleshooting playbooks, papers, notes.
    Use it BEFORE guessing device-specific facts (error codes, register maps, pinouts, procedures, limits).
    Query in the language of the documents (manuals are mostly English); for a Russian question also try English
    keywords. kind: '' (all), manual, datasheet, playbook, paper, note.
    Results look like [doc:ID p.N] Title — section; open more text with library_read."""
    return lib.fmt_hits(lib.search(query, max(1, min(k, 20)), kind if kind in lib.KINDS else ""))


@mcp.tool()
def library_read(doc_id: int, page: int = 0, chunk: int = -1, around: int = 1) -> str:
    """Read a library document: a PDF page (page=N, around=2 also gives the next page), the chunk with id/ordinal
    `chunk` ± around, or with neither the beginning of the document. Cite as [doc:ID p.N]."""
    r = lib.read(doc_id, page=page, chunk=chunk, around=max(1, min(around, 4)))
    d = r["doc"]
    return f"[doc:{d['id']}] {d['title']} ({d['kind']}, {d['pages']} pages, file {d['path']})\n{r['text']}"


@mcp.tool()
def library_list(kind: str = "") -> str:
    """List the documents in the library (id, kind, pages, title)."""
    docs = lib.list_docs(kind if kind in lib.KINDS else "")
    return "\n".join(f"[doc:{d['id']}] {d['kind']} · {d['pages']}p · {d['title']}" for d in docs) or "The library is empty."


if __name__ == "__main__":
    mcp.run()
