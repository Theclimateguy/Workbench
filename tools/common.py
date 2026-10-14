"""Shared helpers for the research MCP servers: one HTTP client, HTML/PDF -> text, output paging."""
from __future__ import annotations

import io
import os
import re
from html.parser import HTMLParser

import httpx

UA = os.environ.get("RESEARCH_UA", "research-chat/1.0 (personal research assistant)")
MAX_CHARS = int(os.environ.get("RESEARCH_TOOL_MAX_CHARS", "12000"))


def client(headers: dict | None = None) -> httpx.Client:
    return httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": UA, **(headers or {})})


def page(text: str, offset: int = 0, limit: int = MAX_CHARS) -> str:
    """Return a window of a long text and tell the model how to get the next one."""
    text = text.strip()
    chunk = text[offset:offset + limit]
    rest = len(text) - offset - len(chunk)
    if rest > 0:
        chunk += f"\n\n[... {rest} more chars; call again with offset={offset + len(chunk)}]"
    return chunk or "[empty]"


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre", "blockquote"}

    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")
        if tag in ("h1", "h2", "h3") and not self.skip:
            self.out.append("#" * int(tag[1]) + " ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(html: str, url: str | None = None) -> str:
    try:  # trafilatura gives far cleaner article text; the stdlib parser is the fallback
        import trafilatura
        text = trafilatura.extract(html, url=url, include_links=False, include_tables=True, favor_recall=True)
        if text and len(text) > 200:
            return text
    except Exception:
        pass
    p = _Text()
    p.feed(html)
    text = "".join(p.out)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def pdf_to_text(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join(f"[page {i + 1}]\n{p.extract_text() or ''}" for i, p in enumerate(reader.pages))


def response_to_text(r: httpx.Response) -> str:
    ctype = r.headers.get("content-type", "")
    if "pdf" in ctype or r.content[:5] == b"%PDF-":
        return pdf_to_text(r.content)
    if "html" in ctype or "<html" in r.text[:2000].lower():
        return html_to_text(r.text, str(r.url))
    return r.text
