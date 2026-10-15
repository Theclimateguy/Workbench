#!/usr/bin/env python3
"""Offline document library (roadmap phase C): manuals, datasheets, playbooks, papers -> searchable chunks.

    /opt/library/docs/<kind>/<file>     originals (PDF, MD, TXT, HTML, DOCX)
    /opt/library/index.db               SQLite: docs, chunks, FTS5 index (BM25), optional embedding vectors
    /opt/library/models/                embedding model cache (fastembed ONNX, CPU; install/fetch-library-model.sh)

Search is hybrid: FTS5 BM25 always; when the embedding model is on disk, cosine similarity over chunk vectors
too, merged by reciprocal rank fusion. Without the model the library still works (keyword search only).
Used by the MCP server `library.py` (agent tools), the web UI (/api/library/*) and `support-ctl library`.

CLI:  library_core.py add FILE... [--kind manual|datasheet|playbook|paper|note]
      library_core.py sync DIR --kind KIND      (index every file of DIR, drop entries whose file is gone)
      library_core.py search QUERY [-k 8] [--kind KIND]
      library_core.py list | rm DOC_ID | reindex | embed | stats
Env:  LIBRARY_DIR (default /opt/library), LIBRARY_EMBED_MODEL
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

LIBRARY_DIR = Path(os.environ.get("LIBRARY_DIR", "/opt/library"))
EMBED_MODEL = os.environ.get("LIBRARY_EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
KINDS = ("manual", "datasheet", "playbook", "paper", "note")
EXTS = {".pdf", ".md", ".markdown", ".txt", ".rst", ".html", ".htm", ".docx", ".csv", ".log", ".json", ".yaml", ".yml"}
CHUNK = 1400          # target chunk size, chars
RRF_K = 60


# ---------- storage ----------

def db() -> sqlite3.Connection:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(LIBRARY_DIR / "index.db", timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS docs (id INTEGER PRIMARY KEY, path TEXT UNIQUE, title TEXT, kind TEXT, sha TEXT,
            pages INTEGER, chars INTEGER, added REAL);
        CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, doc_id INTEGER, ord INTEGER, page INTEGER,
            heading TEXT, text TEXT);
        CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id, ord);
        CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text, heading, title, tokenize='unicode61 remove_diacritics 2');
        CREATE TABLE IF NOT EXISTS vecs (chunk_id INTEGER PRIMARY KEY, v BLOB);
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);""")
    return con


# ---------- extraction ----------

def _html_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<h([1-3])[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", html)
    html = re.sub(r"(?i)</?(p|div|br|li|tr|h\d|section|pre|table)[^>]*>", "\n", html)
    text = re.sub(r"<[^>]+>", "", html)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(a, b)
    return re.sub(r"\n\s*\n+", "\n\n", text)


def extract(path: Path) -> list[tuple[int, str]]:
    """File -> [(page, text)]; page is 0 for formats without pages."""
    ext = path.suffix.lower()
    raw = path.read_bytes()
    if ext == ".pdf" or raw[:5] == b"%PDF-":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        return [(i + 1, p.extract_text() or "") for i, p in enumerate(reader.pages)]
    if ext == ".docx":
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        return [(0, re.sub(r"<[^>]+>", "", re.sub(r"</w:p>", "\n", xml)))]
    text = raw.decode("utf-8", "replace")
    if ext in (".html", ".htm"):
        text = _html_text(text)
    return [(0, text)]


HEAD_RE = re.compile(r"^(#{1,4}\s+.+|(\d+(\.\d+){0,3})\.?\s+[A-ZА-ЯЁ][^\n]{2,80}|[A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9 \-/,&()]{4,70})$")


def chunk_pages(pages: list[tuple[int, str]]) -> list[dict]:
    """Split into ~CHUNK-sized pieces on paragraph boundaries, remembering page and the last heading seen."""
    out, heading = [], ""
    for page, text in pages:
        buf = ""
        for para in re.split(r"\n\s*\n", text.replace("\r", "")):
            para = para.strip()
            if not para:
                continue
            first = para.split("\n", 1)[0].strip()
            if HEAD_RE.match(first) and len(first) < 90 and not re.search(r"\s\d{1,4}$", first):  # not a TOC line
                if buf.strip():                     # a new section starts a new chunk under its own heading
                    out.append({"page": page, "heading": heading, "text": buf.strip()})
                    buf = ""
                heading = first.lstrip("#").strip()
            while len(para) > CHUNK * 1.5:          # very long paragraph (PDF without blank lines)
                cut = para.rfind("\n", 0, CHUNK) if para.rfind("\n", 0, CHUNK) > CHUNK // 2 else CHUNK
                if buf:
                    out.append({"page": page, "heading": heading, "text": buf.strip()}); buf = ""
                out.append({"page": page, "heading": heading, "text": para[:cut].strip()})
                para = para[cut:].strip()
            if len(buf) + len(para) > CHUNK and buf:
                out.append({"page": page, "heading": heading, "text": buf.strip()})
                buf = ""
            buf += para + "\n\n"
        if buf.strip():
            out.append({"page": page, "heading": heading, "text": buf.strip()})
    return [c for c in out if len(c["text"]) > 20]


def title_of(path: Path, pages: list[tuple[int, str]]) -> str:
    for _, text in pages[:2]:
        for ln in text.splitlines()[:15]:
            ln = ln.strip().lstrip("#").strip()
            if 6 <= len(ln) <= 120 and not ln.lower().startswith(("http", "copyright", "©")):
                return ln
    return path.stem


# ---------- embeddings (optional) ----------

_embedder = None


def embedder():
    """fastembed model from LIBRARY_DIR/models, loaded once; None if not installed or not downloaded (offline)."""
    global _embedder
    if _embedder is None:
        _embedder = False
        try:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            try:                                  # fastembed logs a loud ERROR for a missing model; we report it once below
                from loguru import logger
                logger.disable("fastembed")
            except ImportError:
                pass
            from fastembed import TextEmbedding
            _embedder = TextEmbedding(EMBED_MODEL, cache_dir=str(LIBRARY_DIR / "models"), local_files_only=True)
        except Exception as e:  # noqa: BLE001
            print(f"[library] embeddings off: {type(e).__name__}: {str(e)[:160]}", file=sys.stderr)
    return _embedder or None


def _embed(texts: list[str]):
    e = embedder()                        # None without fastembed/numpy or the model: keyword search only
    if not e:
        return None
    import numpy as np
    v = np.array(list(e.embed(texts, batch_size=32)), dtype="float32")
    return v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9)


# ---------- indexing ----------

def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()[:16]


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(LIBRARY_DIR.resolve()))
    except ValueError:
        return str(path.resolve())


def _drop_chunks(con, doc_id: int):
    ids = [r[0] for r in con.execute("SELECT id FROM chunks WHERE doc_id=?", (doc_id,))]
    con.executemany("DELETE FROM fts WHERE rowid=?", [(i,) for i in ids])
    con.executemany("DELETE FROM vecs WHERE chunk_id=?", [(i,) for i in ids])
    con.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))


def _drop(con, doc_id: int):
    _drop_chunks(con, doc_id)
    con.execute("DELETE FROM docs WHERE id=?", (doc_id,))


def index_file(path: Path, kind: str = "manual", force: bool = False) -> dict:
    """(Re)index one file already under LIBRARY_DIR. Unchanged files (same sha) are skipped."""
    path = Path(path)
    sha = _sha(path)
    con = db()
    with con:
        row = con.execute("SELECT id, sha FROM docs WHERE path=?", (_rel(path),)).fetchone()
        if row and row["sha"] == sha and not force:
            return {"id": row["id"], "skipped": True}
        if row:                                   # keep the doc id stable: citations [doc:ID] stay valid
            _drop_chunks(con, row["id"])
    pages = extract(path)
    chunks = chunk_pages(pages)
    title = title_of(path, pages)
    meta = (title, kind, sha, max((p for p, _ in pages), default=0), sum(len(t) for _, t in pages), time.time())
    with con:
        if row:
            doc_id = row["id"]
            con.execute("UPDATE docs SET title=?, kind=?, sha=?, pages=?, chars=?, added=? WHERE id=?", (*meta, doc_id))
        else:
            doc_id = con.execute("INSERT INTO docs(path, title, kind, sha, pages, chars, added) VALUES(?,?,?,?,?,?,?)",
                                 (_rel(path), *meta)).lastrowid
        ids = []
        for i, c in enumerate(chunks):
            cid = con.execute("INSERT INTO chunks(doc_id, ord, page, heading, text) VALUES(?,?,?,?,?)",
                              (doc_id, i, c["page"], c["heading"], c["text"])).lastrowid
            con.execute("INSERT INTO fts(rowid, text, heading, title) VALUES(?,?,?,?)", (cid, c["text"], c["heading"], title))
            ids.append(cid)
    vec = _embed([f"{title}. {c['heading']}\n{c['text']}" for c in chunks]) if chunks else None
    if vec is not None:
        with con:
            con.executemany("INSERT OR REPLACE INTO vecs VALUES(?, ?)", [(i, v.tobytes()) for i, v in zip(ids, vec)])
            con.execute("INSERT OR REPLACE INTO meta VALUES('embed_model', ?)", (EMBED_MODEL,))
    _VEC_CACHE.clear()
    return {"id": doc_id, "title": title, "chunks": len(chunks), "pages": len(pages), "vectors": vec is not None}


def add(src: Path, kind: str = "manual") -> dict:
    """Copy a file into LIBRARY_DIR/docs/<kind>/ and index it."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    src = Path(src)
    if src.suffix.lower() not in EXTS:
        raise ValueError(f"unsupported file type {src.suffix or '(none)'}; supported: {' '.join(sorted(EXTS))}")
    dest_dir = LIBRARY_DIR / "docs" / kind
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / re.sub(r"[^\w.\-() ]+", "_", src.name)
    if src.resolve() != dest.resolve():
        shutil.copyfile(src, dest)
    return index_file(dest, kind)


def sync(directory: Path, kind: str) -> list[dict]:
    """Index every supported file of a directory (incremental) and forget docs of this kind whose file is gone."""
    directory = Path(directory)
    res = [index_file(p, kind) for p in sorted(directory.rglob("*")) if p.is_file() and p.suffix.lower() in EXTS]
    con = db()
    with con:
        for r in con.execute("SELECT id, path FROM docs WHERE kind=?", (kind,)).fetchall():
            p = LIBRARY_DIR / r["path"] if not r["path"].startswith("/") else Path(r["path"])
            if not p.exists():
                _drop(con, r["id"])
    return res


def remove(doc_id: int, delete_file: bool = True) -> bool:
    con = db()
    with con:
        row = con.execute("SELECT path FROM docs WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return False
        _drop(con, doc_id)
    p = LIBRARY_DIR / row["path"]
    if delete_file and p.exists() and LIBRARY_DIR.resolve() in p.resolve().parents:
        p.unlink()
    _VEC_CACHE.clear()
    return True


def embed_missing() -> int:
    """Compute vectors for chunks that have none (after the model was installed)."""
    con = db()
    rows = con.execute("SELECT c.id, c.heading, c.text, d.title FROM chunks c JOIN docs d ON d.id=c.doc_id "
                       "WHERE c.id NOT IN (SELECT chunk_id FROM vecs)").fetchall()
    n = 0
    for i in range(0, len(rows), 256):
        part = rows[i:i + 256]
        vec = _embed([f"{r['title']}. {r['heading']}\n{r['text']}" for r in part])
        if vec is None:
            return n
        with con:
            con.executemany("INSERT OR REPLACE INTO vecs VALUES(?, ?)", [(r["id"], v.tobytes()) for r, v in zip(part, vec)])
            con.execute("INSERT OR REPLACE INTO meta VALUES('embed_model', ?)", (EMBED_MODEL,))
        n += len(part)
    _VEC_CACHE.clear()
    return n


# ---------- search ----------

_VEC_CACHE: dict = {}


def _vectors(con):
    import numpy as np
    if "m" not in _VEC_CACHE:
        rows = con.execute("SELECT chunk_id, v FROM vecs").fetchall()
        _VEC_CACHE["ids"] = [r[0] for r in rows]
        _VEC_CACHE["m"] = np.frombuffer(b"".join(r[1] for r in rows), dtype="float32").reshape(len(rows), -1) if rows else None
    return _VEC_CACHE["ids"], _VEC_CACHE["m"]


def _fts_query(q: str) -> str:
    words = [w for w in re.findall(r"[\w\-.]+", q.lower()) if len(w) > 1]
    terms = []
    for w in words[:24]:
        w = w.strip(".-")
        if not w:
            continue
        terms.append(f'"{w}"*' if len(w) >= 4 and not re.search(r"\d", w) else f'"{w}"')
    return " OR ".join(terms)


def search(query: str, k: int = 8, kind: str = "") -> list[dict]:
    con = db()
    kinds = (kind,) if kind else KINDS
    ranked: dict[int, float] = {}
    q = _fts_query(query)
    if q:
        rows = con.execute("SELECT f.rowid FROM fts f JOIN chunks c ON c.id=f.rowid JOIN docs d ON d.id=c.doc_id "
                           f"WHERE fts MATCH ? AND d.kind IN ({','.join('?' * len(kinds))}) ORDER BY bm25(fts, 1.0, 2.0, 1.5) LIMIT 50",
                           (q, *kinds)).fetchall()
        for rank, r in enumerate(rows):
            ranked[r[0]] = ranked.get(r[0], 0) + 1 / (RRF_K + rank)
    if embedder():
        ids, m = _vectors(con)
        if m is not None:
            qv = _embed([query])[0]
            sims = m @ qv
            allowed = {r[0] for r in con.execute(f"SELECT c.id FROM chunks c JOIN docs d ON d.id=c.doc_id WHERE d.kind IN ({','.join('?' * len(kinds))})", kinds)} if kind else None
            order = sims.argsort()[::-1]
            rank = 0
            for j in order[:400]:
                cid = ids[j]
                if allowed is not None and cid not in allowed:
                    continue
                ranked[cid] = ranked.get(cid, 0) + 1 / (RRF_K + rank)
                rank += 1
                if rank >= 50:
                    break
    top = sorted(ranked.items(), key=lambda x: -x[1])[:k]
    out = []
    for cid, score in top:
        r = con.execute("SELECT c.id, c.doc_id, c.ord, c.page, c.heading, c.text, d.title, d.kind, d.path FROM chunks c "
                        "JOIN docs d ON d.id=c.doc_id WHERE c.id=?", (cid,)).fetchone()
        if r:
            out.append({**dict(r), "score": round(score, 4)})
    return out


def read(doc_id: int, page: int = 0, chunk: int = -1, around: int = 1, max_chars: int = 12000) -> dict:
    """Text of a document: one page (PDF), or chunk index ± around, or the beginning."""
    con = db()
    d = con.execute("SELECT * FROM docs WHERE id=?", (doc_id,)).fetchone()
    if not d:
        raise ValueError(f"no document {doc_id}")
    if page:
        rows = con.execute("SELECT ord, page, heading, text FROM chunks WHERE doc_id=? AND page BETWEEN ? AND ? ORDER BY ord",
                           (doc_id, page - around + 1 if around > 1 else page, page + around - 1 if around > 1 else page)).fetchall()
    elif chunk >= 0:
        rows = con.execute("SELECT ord, page, heading, text FROM chunks WHERE doc_id=? AND ord BETWEEN ? AND ? ORDER BY ord",
                           (doc_id, chunk - around, chunk + around)).fetchall()
    else:
        rows = con.execute("SELECT ord, page, heading, text FROM chunks WHERE doc_id=? ORDER BY ord LIMIT 8", (doc_id,)).fetchall()
    text, last_page = "", None
    for r in rows:
        if r["page"] and r["page"] != last_page:
            text += f"\n[page {r['page']}]\n"
            last_page = r["page"]
        text += r["text"] + "\n\n"
    return {"doc": dict(d), "text": text[:max_chars] + ("\n[... truncated]" if len(text) > max_chars else "")}


def list_docs(kind: str = "") -> list[dict]:
    con = db()
    q = "SELECT d.*, (SELECT count(*) FROM chunks c WHERE c.doc_id=d.id) AS chunks FROM docs d"
    rows = con.execute(q + (" WHERE kind=?" if kind else "") + " ORDER BY kind, title", (kind,) if kind else ()).fetchall()
    return [dict(r) for r in rows]


def stats() -> dict:
    con = db()
    return {"docs": con.execute("SELECT count(*) FROM docs").fetchone()[0],
            "chunks": con.execute("SELECT count(*) FROM chunks").fetchone()[0],
            "vectors": con.execute("SELECT count(*) FROM vecs").fetchone()[0],
            "by_kind": {r[0]: r[1] for r in con.execute("SELECT kind, count(*) FROM docs GROUP BY kind")},
            "embeddings": bool(embedder()), "model": EMBED_MODEL, "dir": str(LIBRARY_DIR)}


def fmt_hits(hits: list[dict], snippet: int = 600) -> str:
    out = []
    for h in hits:
        where = f"p.{h['page']}" if h["page"] else f"chunk {h['ord']}"
        head = f" — {h['heading']}" if h["heading"] else ""
        body = " ".join(h["text"].split())
        out.append(f"[doc:{h['doc_id']} {where}] {h['title']}{head} ({h['kind']})\n{body[:snippet]}{'…' if len(body) > snippet else ''}")
    return "\n\n".join(out) or "No matches in the library."


def main(argv=None):
    ap = argparse.ArgumentParser(description="offline document library")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("files", nargs="+"); a.add_argument("--kind", default="manual", choices=KINDS)
    s = sub.add_parser("sync"); s.add_argument("dir"); s.add_argument("--kind", required=True, choices=KINDS)
    q = sub.add_parser("search"); q.add_argument("query", nargs="+"); q.add_argument("-k", type=int, default=8); q.add_argument("--kind", default="")
    r = sub.add_parser("rm"); r.add_argument("doc_id", type=int)
    sub.add_parser("list"); sub.add_parser("stats"); sub.add_parser("embed"); sub.add_parser("reindex")
    args = ap.parse_args(argv)
    if args.cmd == "add":
        for f in args.files:
            print(f, json.dumps(add(Path(f), args.kind), ensure_ascii=False))
    elif args.cmd == "sync":
        res = sync(Path(args.dir), args.kind)
        print(f"{len(res)} files, {sum(1 for x in res if not x.get('skipped'))} (re)indexed")
    elif args.cmd == "search":
        print(fmt_hits(search(" ".join(args.query), args.k, args.kind)))
    elif args.cmd == "rm":
        print("removed" if remove(args.doc_id) else "not found")
    elif args.cmd == "list":
        for d in list_docs():
            print(f"{d['id']:>4}  {d['kind']:<9} {d['pages']:>4}p {d['chunks']:>5}ch  {d['title'][:70]}  ({d['path']})")
    elif args.cmd == "stats":
        print(json.dumps(stats(), ensure_ascii=False, indent=1))
    elif args.cmd == "embed":
        print(f"{embed_missing()} chunks embedded")
    elif args.cmd == "reindex":
        for d in list_docs():
            p = LIBRARY_DIR / d["path"]
            if p.exists():
                print(d["path"], json.dumps(index_file(p, d["kind"], force=True), ensure_ascii=False))


if __name__ == "__main__":
    main()
