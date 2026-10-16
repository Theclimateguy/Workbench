#!/usr/bin/env python3
"""MCP server `scholar`: journal literature by DOI: metadata, open-access copies, references and citing works.

OpenAlex (https://api.openalex.org, no key needed) is the main index; Crossref is the fallback for DOI metadata,
abstracts and reference lists that OpenAlex lacks. Without a key OpenAlex shares a small free daily budget per IP
(a search costs ~10x a lookup), so a personal key is worth setting.

Env: OPENALEX_API_KEY (optional, free at openalex.org/settings/api: own daily budget),
     OPENALEX_MAILTO (optional contact address: OpenAlex/Crossref polite pools; never hardcoded)
"""
from __future__ import annotations

import os
import re

from mcp.server.fastmcp import FastMCP

from common import client, get, html_to_text, page as window

OPENALEX = "https://api.openalex.org"
CROSSREF = "https://api.crossref.org"
API_KEY = os.environ.get("OPENALEX_API_KEY", "")
MAILTO = os.environ.get("OPENALEX_MAILTO", "")
DOI = re.compile(r"10\.\d{4,9}/[^\s\"<>]+")
OAID = re.compile(r"\b(W\d{4,})\b", re.I)
LIST_FIELDS = "id,doi,display_name,publication_year,authorships,primary_location,open_access,cited_by_count,type"
mcp = FastMCP("scholar")


class Budget(Exception):
    pass


def _ref(s: str) -> tuple[str, str]:
    """A DOI (bare, doi:, https://doi.org/...) or an OpenAlex work id (W123, openalex.org URL) -> (kind, id)."""
    s = (s or "").strip()
    m = DOI.search(s)
    if m:
        return "doi", m.group(0).rstrip(".,;)]}").lower()
    m = OAID.search(s)
    if m:
        return "openalex", m.group(1).upper()
    raise ValueError(f"not a DOI or OpenAlex work id: {s!r} (examples: 10.1038/nature14956, W1789155650)")


def _oa(path: str, **params) -> dict | None:
    """GET an OpenAlex endpoint -> JSON, or None for 404."""
    if API_KEY:
        params["api_key"] = API_KEY
    if MAILTO:
        params["mailto"] = MAILTO
    with client() as c:
        r = get(c, OPENALEX + path, params=params)
    if r.status_code == 404:
        return None
    if r.status_code == 429:
        raise Budget("OpenAlex rate limit / daily budget reached" + ("" if API_KEY else
                     " (no OPENALEX_API_KEY: the free budget is shared per IP)") + "; try again later")
    r.raise_for_status()
    return r.json()


def _crossref(doi: str) -> dict | None:
    with client() as c:
        r = get(c, f"{CROSSREF}/works/{doi}", params={"mailto": MAILTO} if MAILTO else None)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json().get("message")


def _abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    words = sorted((i, w) for w, pos in inv.items() for i in pos)
    return re.sub(r"^abstract[.:]?\s+", "", " ".join(w for _, w in words), flags=re.I)


def _jats(s: str) -> str:
    """Crossref abstracts are JATS XML."""
    if not (s or "").strip():
        return ""
    s = re.sub(r"<jats:title>.*?</jats:title>", "", s, flags=re.S)
    s = " ".join(html_to_text(re.sub(r"<(/?)jats:", r"<\1", s)).split())
    return re.sub(r"^abstract[.:]?\s+", "", s, flags=re.I)


def _authors(names: list[str], n: int) -> str:
    return ", ".join(names[:n]) + (" et al." if len(names) > n else "")


def _tag(w: dict) -> str:
    doi = (w.get("doi") or "").removeprefix("https://doi.org/").lower()
    return f"doi:{doi}" if doi else "openalex:" + (w.get("id") or "").rsplit("/", 1)[-1]


def _venue(w: dict) -> str:
    return ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""


def _oa_line(w: dict) -> str:
    oa = w.get("open_access") or {}
    best = w.get("best_oa_location") or {}
    status = oa.get("oa_status") or ("open" if oa.get("is_oa") else "closed")
    url = best.get("pdf_url") or best.get("landing_page_url") or oa.get("oa_url") or ""
    kind = "PDF" if best.get("pdf_url") else "landing page" if url else ""
    lic = best.get("license") or ""
    return f"OA: {status}" + (f" · {kind} {url}" if url else "") + (f" · {lic}" if lic else "") + \
           (f" · version {best['version']}" if best.get("version") else "")


def _item(w: dict, abstract: int = 0) -> str:
    """One work as a compact list entry (search, references, cited_by)."""
    names = [a.get("author", {}).get("display_name", "") for a in w.get("authorships") or []]
    oa = w.get("open_access") or {}
    bits = [_authors(names, 4), str(w.get("publication_year") or ""), _venue(w), f"cited by {w.get('cited_by_count', 0)}",
            "OA " + (oa.get("oa_status") or "") if oa.get("is_oa") else "closed"]
    out = f"[{_tag(w)}] {' '.join((w.get('display_name') or '(no title)').split())}\n  " + " · ".join(b for b in bits if b)
    if abstract:
        text = _abstract(w.get("abstract_inverted_index"))
        if text:
            out += "\n  " + text[:abstract] + ("…" if len(text) > abstract else "")
    return out


def _resolve(work: str) -> dict:
    """DOI / OpenAlex id -> OpenAlex work with id, title, counts (for references / cited_by)."""
    kind, wid = _ref(work)
    w = _oa(f"/works/{'doi:' + wid if kind == 'doi' else wid}",
            select="id,doi,display_name,publication_year,cited_by_count,referenced_works_count")
    if not w:
        raise LookupError(f"{work} is not in OpenAlex")
    return w


def _listing(head: str, data: dict, page_no: int, per: int) -> str:
    total = (data.get("meta") or {}).get("count") or 0
    items = data.get("results") or []
    if not items:
        return head + "\n\nNo results."
    first = (page_no - 1) * per + 1
    out = f"{head}\n{total} in total; {first}-{first + len(items) - 1} shown\n\n" + "\n\n".join(_item(w) for w in items)
    if first + len(items) - 1 < total:
        out += f"\n\n[more: call again with page={page_no + 1}]"
    return out


@mcp.tool()
def doi_lookup(doi: str) -> str:
    """Resolve a DOI (or OpenAlex work id W...) to the paper's record: title, authors, year, venue, type, abstract
    (if indexed), citation counts, open-access status and the best free PDF / landing page (read it with fetch_url).
    Accepts 10.xxxx/..., doi:..., https://doi.org/... URLs. Crossref is used when OpenAlex does not have the DOI."""
    kind, wid = _ref(doi)
    w, note = None, ""
    try:
        w = _oa(f"/works/{'doi:' + wid if kind == 'doi' else wid}")
    except Exception as e:  # rate limit, outage: Crossref still answers for DOIs
        if kind != "doi":
            raise
        note = f"(OpenAlex unavailable: {e}; metadata from Crossref)"
    if w:
        names = [a.get("author", {}).get("display_name", "") for a in w.get("authorships") or []]
        loc = w.get("primary_location") or {}
        src = loc.get("source") or {}
        bib = w.get("biblio") or {}
        where = ", ".join(x for x in (_venue(w), bib.get("volume") and f"vol. {bib['volume']}",
                                      bib.get("issue") and f"no. {bib['issue']}",
                                      bib.get("first_page") and f"pp. {bib['first_page']}" +
                                      (f"-{bib['last_page']}" if bib.get("last_page") else "")) if x)
        tag = _tag(w)
        lines = [f"[{tag}] {' '.join((w.get('display_name') or '(no title)').split())}",
                 f"  {_authors(names, 30)}",
                 f"  {w.get('publication_date') or w.get('publication_year') or ''} · {where}"
                 f"{' · ' + src['host_organization_name'] if src.get('host_organization_name') else ''} · {w.get('type', '')}",
                 f"  https://doi.org/{tag[4:]}" if tag.startswith("doi:") else f"  {w.get('id')}",
                 f"  {_oa_line(w)}",
                 f"  cited by {w.get('cited_by_count', 0)} · {w.get('referenced_works_count', 0)} references"
                 f" · OpenAlex {(w.get('id') or '').rsplit('/', 1)[-1]}"]
        topics = [t.get("display_name") for t in (w.get("topics") or [])[:4]]
        if topics:
            lines.append("  topics: " + "; ".join(topics))
        abstract = _abstract(w.get("abstract_inverted_index"))
        if not abstract and tag.startswith("doi:"):
            try:  # OpenAlex drops some publishers' abstracts; Crossref often has the deposited one
                abstract = _jats((_crossref(tag[4:]) or {}).get("abstract", ""))
            except Exception:
                pass
        lines += ["", "Abstract: " + abstract if abstract else "No abstract indexed (open the OA copy or the DOI page)."]
        return window("\n".join(lines))
    if kind != "doi":
        return f"OpenAlex work {wid} not found."
    m = _crossref(wid)
    if not m:
        return f"DOI {wid} not found in OpenAlex or Crossref."
    names = [" ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name", "") for a in m.get("author") or []]
    issued = "-".join(str(p) for p in ((m.get("issued") or {}).get("date-parts") or [[]])[0])
    venue = "; ".join(m.get("container-title") or [])
    lines = [f"[doi:{wid}] {' '.join(' '.join(m.get('title') or ['(no title)']).split())}",
             f"  {_authors(names, 30)}",
             f"  {issued} · {venue} · {m.get('publisher', '')} · {m.get('type', '')}",
             f"  https://doi.org/{wid}",
             "  OA: unknown (Crossref has no OA data; check the DOI page)",
             f"  cited by {m.get('is-referenced-by-count', 0)} (Crossref) · {m.get('references-count', 0)} references"]
    if note:
        lines.append("  " + note)
    abstract = _jats(m.get("abstract", ""))
    lines += ["", "Abstract: " + abstract if abstract else "No abstract deposited."]
    return window("\n".join(lines))


@mcp.tool()
def paper_search(query: str, year_from: int = 0, year_to: int = 0, max_results: int = 10, sort: str = "relevance",
                 open_access: bool = False, page: int = 1) -> str:
    """Search journal articles, books and preprints of all fields (OpenAlex, ~250M works) by keywords in
    title and abstract. year_from / year_to bound the publication year (0 = open).
    sort: 'relevance', 'cited' (most cited first) or 'date' (newest first). open_access=true keeps only works
    with a free copy. Results are snippets: open one with doi_lookup."""
    per = max(1, min(max_results, 25))
    filters = [f"title_and_abstract.search:{query.replace(',', ' ')}"]  # full-text `search` is far noisier
    if year_from or year_to:
        filters.append(f"publication_year:{year_from or ''}-{year_to or ''}")
    if open_access:
        filters.append("is_oa:true")
    params = {"filter": ",".join(filters), "per_page": per, "page": max(1, page),
              "sort": {"cited": "cited_by_count:desc", "date": "publication_date:desc"}.get(sort, "relevance_score:desc"),
              "select": LIST_FIELDS + ",abstract_inverted_index"}
    data = _oa("/works", **params) or {}
    total = (data.get("meta") or {}).get("count") or 0
    items = data.get("results") or []
    if not items:
        return "No results."
    out = f"{total} works match\n\n" + "\n\n".join(_item(w, 300) for w in items)
    if params["page"] * per < total:
        out += f"\n\n[more: call again with page={params['page'] + 1}]"
    return out


@mcp.tool()
def references(work: str, max_results: int = 25, page: int = 1) -> str:
    """The works a paper cites (its reference list), most cited first. work: a DOI or OpenAlex id (W...).
    Falls back to the Crossref reference list when OpenAlex has none."""
    w = _resolve(work)
    per = max(1, min(max_results, 50))
    title = f"[{_tag(w)}] {w.get('display_name') or ''} ({w.get('publication_year') or ''})"
    if w.get("referenced_works_count"):
        data = _oa("/works", filter=f"cited_by:{w['id'].rsplit('/', 1)[-1]}", sort="cited_by_count:desc",
                   per_page=per, page=max(1, page), select=LIST_FIELDS) or {}
        if data.get("results"):
            return _listing(f"References of {title}", data, max(1, page), per)
    doi = _tag(w)[4:] if _tag(w).startswith("doi:") else ""
    refs = ((_crossref(doi) or {}).get("reference") or []) if doi else []
    if not refs:
        return f"References of {title}\n\nNo reference list in OpenAlex or Crossref (the publisher may not deposit it)."
    start = (max(1, page) - 1) * per
    chunk = refs[start:start + per]
    lines = [f"References of {title} (Crossref, in the paper's order)", f"{len(refs)} in total; {start + 1}-{start + len(chunk)} shown", ""]
    for r in chunk:
        text = r.get("unstructured") or ", ".join(str(r[k]) for k in ("author", "article-title", "journal-title", "year") if r.get(k))
        lines.append((f"[doi:{r['DOI'].lower()}] " if r.get("DOI") else "- ") + " ".join(text.split())[:400])
    if start + len(chunk) < len(refs):
        lines.append(f"\n[more: call again with page={max(1, page) + 1}]")
    return "\n".join(lines)


@mcp.tool()
def cited_by(work: str, sort: str = "cited", max_results: int = 25, page: int = 1) -> str:
    """The works that cite a paper (forward citations). work: a DOI or OpenAlex id (W...).
    sort: 'cited' (most cited first) or 'date' (newest first, for the latest follow-up work)."""
    w = _resolve(work)
    per = max(1, min(max_results, 50))
    title = f"[{_tag(w)}] {w.get('display_name') or ''} ({w.get('publication_year') or ''})"
    data = _oa("/works", filter=f"cites:{w['id'].rsplit('/', 1)[-1]}",
               sort="publication_date:desc" if sort == "date" else "cited_by_count:desc",
               per_page=per, page=max(1, page), select=LIST_FIELDS) or {}
    return _listing(f"Works citing {title}", data, max(1, page), per)


if __name__ == "__main__":
    mcp.run()
