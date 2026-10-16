"""Source ledger of the research chat (used by app.py, not an MCP server): what the assistant actually read.

Search results are snippets and are not recorded. Every successful result of a reading tool (fetch_url, arxiv_paper,
doi_lookup, zenodo_record, gh_repo, gh_read, library_read, download) becomes source S<n> of its chat: reference, URL, title,
first and last fetch time, the parts that were read and a sha256 of the text read (of the file, for downloads) as
its version. Paged documents keep their parts in document coordinates (the `offset` of the tool that read them),
so source_read(n, offset) and the tool itself agree, and unread stretches come with the exact call that opens them.
The UI links [S<n>] and lists the sources; the Markdown export appends them.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time

from common import MAX_CHARS, page

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (id INTEGER PRIMARY KEY, chat_id TEXT, n INTEGER, kind TEXT, ref TEXT, url TEXT,
    title TEXT, tool TEXT, reopen TEXT, first REAL, fetched REAL, total INTEGER, sha TEXT, UNIQUE(chat_id, ref));
CREATE TABLE IF NOT EXISTS source_parts (source_id INTEGER, loc TEXT, pos INTEGER, paged INTEGER, text TEXT,
    fetched REAL, PRIMARY KEY(source_id, loc));
"""
READ_TOOLS = {"fetch_url", "arxiv_paper", "doi_lookup", "zenodo_record", "gh_repo", "gh_read", "library_read", "download"}
MORE = re.compile(r"\n\n\[\.\.\. (\d+) more chars; call again with offset=(\d+)\]\s*$")  # common.page()
ARXIV_ID = re.compile(r"(\d{4}\.\d{4,5}(v\d+)?|[a-z\-]+(\.[A-Z]{2})?/\d{7}(v\d+)?)")


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _window(text: str, offset: int) -> tuple[str, int]:
    """A window returned by common.page() -> (window text, total chars of the document)."""
    m = MORE.search(text)
    if m:
        return text[:m.start()], int(m.group(2)) + int(m.group(1))
    return text, offset + len(text)


def _title(text: str) -> str:
    for ln in text.splitlines():
        ln = ln.strip().lstrip("#").strip()
        if len(ln) > 3:
            return ln[:160]
    return ""


def _paged(loc_pos: int, text: str) -> dict:
    return {"loc": str(loc_pos), "pos": loc_pos, "paged": 1, "text": text}


def _whole(loc: str, text: str, pos: int = 0) -> dict:
    return {"loc": loc, "pos": pos, "paged": 0, "text": text}


def _call(tool: str, **args) -> str:
    return f"{tool}({', '.join(f'{k}={json.dumps(v, ensure_ascii=False)}' for k, v in args.items())})"


def parse(name: str, args: dict, result: str) -> dict | None:
    """Tool call + successful result -> {kind, ref, url, title, total, reopen, parts}, or None when the result
    is not a document read (an error, a directory listing, an offset past the end)."""
    if name not in READ_TOOLS or result.startswith("ERROR") or result.rstrip().endswith("[empty]"):
        return None
    off = _int(args.get("offset"))
    if name == "fetch_url":
        if not result.startswith("URL: "):
            return None                                     # HTTP error, bad scheme
        head, _, rest = result.partition("\n\n")
        url = head[5:].strip()
        body, total = _window(rest, off)
        return {"kind": "web", "ref": url, "url": url, "title": _title(body) if off == 0 else "", "total": total,
                "reopen": _call("fetch_url", url=url), "parts": [_paged(off, body)]}
    if name == "arxiv_paper":
        m = ARXIV_ID.search(str(args.get("arxiv_id", "")))
        if not m or "not found" in result[:200]:
            return None
        aid = m.group(1)
        src = {"kind": "paper", "ref": f"arxiv:{aid}", "url": f"https://arxiv.org/abs/{aid}", "title": "", "total": None,
               "reopen": _call("arxiv_paper", arxiv_id=aid, full_text=True)}
        if off:
            body, src["total"] = _window(result, off)
            return {**src, "parts": [_paged(off, body)]}
        first = result.split("\n", 1)[0]
        src["title"] = re.sub(r"^\[[^\]]+\]\s*", "", first)[:160]
        meta, sep, win = result.partition("\n\n--- full text ---\n")
        parts = [_whole("abstract", meta, -1)]
        if sep:
            body, src["total"] = _window(win, 0)
            parts.append(_paged(0, body))
        return {**src, "parts": parts}
    if name == "doi_lookup":
        m = re.match(r"\[(doi:(\S+)|openalex:(W\d+))\]\s*(.*)", result)
        if not m:
            return None                                     # not found
        url = f"https://doi.org/{m.group(2)}" if m.group(2) else f"https://openalex.org/{m.group(3)}"
        return {"kind": "paper", "ref": m.group(1), "url": url, "title": m.group(4)[:160], "total": None,
                "reopen": "", "parts": [_whole("record", result)]}
    if name == "zenodo_record":
        m = re.match(r"\[zenodo:(\d+)\]\s*(.*)", result)
        if not m:
            return None
        return {"kind": "dataset", "ref": f"zenodo:{m.group(1)}", "url": f"https://zenodo.org/records/{m.group(1)}",
                "title": m.group(2)[:160], "total": None, "reopen": "", "parts": [_whole("record", result)]}
    if name == "gh_repo":
        repo = str(args.get("repo", "")).strip("/")
        return {"kind": "code", "ref": f"github:{repo}", "url": f"https://github.com/{repo}", "title": repo,
                "total": None, "reopen": "", "parts": [_whole("overview", result)]}
    if name == "gh_read":
        lines = result.strip().splitlines()
        if not lines or all(ln.startswith(("dir ", "file ")) for ln in lines):
            return None                                     # a directory listing, not a document
        repo, path, ref = str(args.get("repo", "")).strip("/"), str(args.get("path", "")).strip("/"), str(args.get("ref", ""))
        body, total = _window(result, off)
        return {"kind": "code", "ref": f"github:{repo}/{path}" + (f"@{ref}" if ref else ""),
                "url": f"https://github.com/{repo}/blob/{ref or 'HEAD'}/{path}", "title": f"{repo}/{path}", "total": total,
                "reopen": _call("gh_read", repo=repo, path=path, **({"ref": ref} if ref else {})), "parts": [_paged(off, body)]}
    if name == "library_read":
        m = re.match(r"\[doc:(\d+)\]\s*(.*?)(?: \([^()]*pages[^()]*\))?\n", result)
        if not m:
            return None
        pg, ch = _int(args.get("page")), _int(args.get("chunk"), -1)
        loc = f"p.{pg}" if pg else f"chunk {ch}" if ch >= 0 else "start"
        return {"kind": "library", "ref": f"doc:{m.group(1)}", "url": "", "title": m.group(2)[:160], "total": None,
                "reopen": "", "parts": [_whole(loc, result, pg or max(ch, 0))]}
    if name == "download":
        m = re.match(r"saved (\S+) .*\nsha256 ([0-9a-f]{64})", result)
        if not m:
            return None
        url = str(args.get("url", ""))
        return {"kind": "data", "ref": url, "url": url, "title": f"/ws/{m.group(1)}", "total": None, "reopen": "",
                "parts": [_whole("file", result)], "sha": m.group(2)}
    return None


def _parts(con: sqlite3.Connection, sid: int) -> list[sqlite3.Row]:
    return con.execute("SELECT loc, pos, paged, text FROM source_parts WHERE source_id=? ORDER BY paged, pos, loc",
                       (sid,)).fetchall()


def _spans(parts) -> list[list[int]]:
    """Merged [start, end) document ranges read of a paged source."""
    spans: list[list[int]] = []
    for p in parts:
        if not p["paged"]:
            continue
        a, b = p["pos"], p["pos"] + len(p["text"])
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    return spans


def coverage(parts, total: int | None) -> str:
    """What of a source was read: 'all', 'chars 0-12000 of 36867', 'abstract', 'p.3, p.4'."""
    out, spans = [], _spans(parts)
    if spans:
        out.append("all" if total and spans == [[0, total]] else
                   "chars " + ", ".join(f"{a}-{b}" for a, b in spans) + (f" of {total}" if total else ""))
    out += [p["loc"] for p in parts if not p["paged"] and p["loc"] not in ("record", "overview", "file")]
    return "; ".join(out) or "all"


def record(con: sqlite3.Connection, chat_id: str, tool: str, src: dict) -> dict:
    """Store one read; returns the source summary (n, title, url, ...)."""
    now = time.time()
    row = con.execute("SELECT id, n, title, total FROM sources WHERE chat_id=? AND ref=?", (chat_id, src["ref"])).fetchone()
    if row:
        sid, n = row["id"], row["n"]
        con.execute("UPDATE sources SET fetched=?, title=?, total=? WHERE id=?",
                    (now, row["title"] or src["title"], src["total"] or row["total"], sid))
    else:
        n = con.execute("SELECT COALESCE(MAX(n), 0) + 1 FROM sources WHERE chat_id=?", (chat_id,)).fetchone()[0]
        sid = con.execute("INSERT INTO sources(chat_id, n, kind, ref, url, title, tool, reopen, first, fetched, total) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (chat_id, n, src["kind"], src["ref"], src["url"], src["title"],
                                                           tool, src["reopen"], now, now, src["total"])).lastrowid
    for p in src["parts"]:
        con.execute("INSERT OR REPLACE INTO source_parts VALUES(?,?,?,?,?,?)", (sid, p["loc"], p["pos"], p["paged"], p["text"], now))
    text = "\n".join(p["text"] for p in _parts(con, sid))
    con.execute("UPDATE sources SET sha=? WHERE id=?", (src.get("sha") or hashlib.sha256(text.encode()).hexdigest(), sid))
    return next(s for s in list_sources(con, chat_id) if s["n"] == n)


def list_sources(con: sqlite3.Connection, chat_id: str) -> list[dict]:
    out = []
    for r in con.execute("SELECT * FROM sources WHERE chat_id=? ORDER BY n", (chat_id,)).fetchall():
        parts = _parts(con, r["id"])
        out.append({"n": r["n"], "kind": r["kind"], "ref": r["ref"], "url": r["url"], "title": r["title"], "tool": r["tool"],
                    "first": r["first"], "fetched": r["fetched"], "read": sum(len(p["text"]) for p in parts),
                    "total": r["total"], "coverage": coverage(parts, r["total"]), "sha": r["sha"]})
    return out


def fmt_list(con: sqlite3.Connection, chat_id: str) -> str:
    return "\n".join(f"S{s['n']} · {s['kind']} · {s['title'] or s['ref']}\n   {s['url'] or s['ref']} · "
                     f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(s['fetched']))} · read: {s['coverage']}"
                     for s in list_sources(con, chat_id)) or "No sources read in this chat yet."


def brief(con: sqlite3.Connection, chat_id: str, limit: int = 40) -> str:
    """One line per source for the system prompt, so the model knows what it has (and has not) read."""
    return "\n".join(f"S{s['n']} {s['title'] or s['ref']} ({s['ref']}) — read: {s['coverage']}"
                     for s in list_sources(con, chat_id)[-limit:])


def read(con: sqlite3.Connection, chat_id: str, n: int, offset: int = 0) -> str:
    """Saved text of S<n>. Paged sources: the window [offset, offset + MAX_CHARS) in document coordinates, with
    unread stretches named together with the call that reads them."""
    r = con.execute("SELECT * FROM sources WHERE chat_id=? AND n=?", (chat_id, n)).fetchone()
    if not r:
        return f"No source S{n} in this chat (sources_list shows them)."
    parts = _parts(con, r["id"])
    whole = [p for p in parts if not p["paged"]]
    paged = [p for p in parts if p["paged"]]
    head = (f"S{n} · {r['title'] or r['ref']}\n{r['url'] or r['ref']} · fetched "
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(r['fetched']))} · read: {coverage(parts, r['total'])}\n\n")
    if not paged:
        return (head if offset == 0 else "") + page("\n\n".join(f"[{p['loc']}]\n{p['text']}" for p in whole), offset)

    def gap(a: int, b: int) -> str:
        how = f"; read it with {r['reopen'][:-1]}, offset={a})" if r["reopen"] else ""
        return f"\n[chars {a}-{b} were not read yet{how}]\n"

    out, at, stop = [], offset, offset + MAX_CHARS
    if offset == 0:
        out += [p["text"].strip() + "\n\n--- text ---\n" for p in whole]
    for p in paged:
        a, b = p["pos"], p["pos"] + len(p["text"])
        if b <= at:
            continue
        if a >= stop:
            break
        if a > at:
            out.append(gap(at, a))
            at = a
        piece = p["text"][at - a:stop - a]
        out.append(piece)
        at += len(piece)
    total = r["total"] or max(b for _, b in _spans(paged))
    if at < min(stop, total):  # nothing read from here on within this window
        out.append(gap(at, min((p["pos"] for p in paged if p["pos"] > at), default=total)))
    elif at < total:
        out.append(f"\n\n[... more: source_read(n={n}, offset={at})]")
    return (head if offset == 0 else f"S{n} from offset {offset}\n\n") + "".join(out)


def delete(con: sqlite3.Connection, chat_id: str):
    con.execute("DELETE FROM source_parts WHERE source_id IN (SELECT id FROM sources WHERE chat_id=?)", (chat_id,))
    con.execute("DELETE FROM sources WHERE chat_id=?", (chat_id,))
