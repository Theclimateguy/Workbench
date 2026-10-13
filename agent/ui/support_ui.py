#!/usr/bin/env python3
"""Support agent web UI: a thin, dependency-light front over the headless OpenCode server.

Why not `opencode web`: its bundled SPA has an open regression (anomalyco/opencode#30478) and
we want a UI that only exposes what a support operator needs — chat, tool calls, approval
buttons, targets, audit — and keeps working across OpenCode upgrades (it uses the stable
HTTP API only).

Run:  uvicorn support_ui:app --host 0.0.0.0 --port 8080   (see stack/systemd/support-ui.service)
      + a second instance with --ssl-keyfile/--ssl-certfile on 8443 (support-ui-tls.service):
      microphone access and PWA install only work on HTTPS; the local CA is served at /ca.crt.
Env:  OPENCODE_URL (default http://127.0.0.1:4096), OPENCODE_SERVER_PASSWORD / _USERNAME,
      SUPPORT_UI_PASSWORD (basic auth for this UI; defaults to the OpenCode password),
      SUPPORT_WORKSPACE (default /opt/support/workspace), SUPPORT_TARGETS_DIR, SUPPORT_AUDIT_LOG,
      SUPPORT_UI_ASSETS (default /opt/support/ui-assets: vendor/ + models/ for browser-side STT),
      SUPPORT_TLS_DIR (default /etc/support/tls)
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

OPENCODE_URL = os.environ.get("OPENCODE_URL", "http://127.0.0.1:4096")
OC_USER = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
OC_PASS = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
UI_USER = os.environ.get("SUPPORT_UI_USERNAME", "support")
UI_PASS = os.environ.get("SUPPORT_UI_PASSWORD", OC_PASS)
WORKSPACE = os.environ.get("SUPPORT_WORKSPACE", "/opt/support/workspace")
TARGETS_DIR = Path(os.environ.get("SUPPORT_TARGETS_DIR", "/opt/support/targets"))
AUDIT_LOG = Path(os.environ.get("SUPPORT_AUDIT_LOG", "/var/log/support/audit.jsonl"))
LLM_URL = os.environ.get("LLM_URL", "http://127.0.0.1:8000")
STATIC_DIR = Path(__file__).parent / "static"
ASSETS_DIR = Path(os.environ.get("SUPPORT_UI_ASSETS", "/opt/support/ui-assets"))
TLS_DIR = Path(os.environ.get("SUPPORT_TLS_DIR", "/etc/support/tls"))
INDEX_HTML = (Path(__file__).parent / "index.html").read_text()
PUBLIC_PATHS = ("/ca.crt", "/sw.js", "/static/manifest.json", "/static/icons/")   # no login needed

app = FastAPI(title="support-ui")
client = httpx.AsyncClient(base_url=OPENCODE_URL, auth=(OC_USER, OC_PASS) if OC_PASS else None,
                           timeout=httpx.Timeout(30.0, read=120.0))


# ---------- auth ----------

def _is_local(request: Request) -> bool:
    # Someone sitting at the GX10 itself (desktop launcher) is trusted; LAN/hotspot clients must log in.
    return bool(request.client) and request.client.host in ("127.0.0.1", "::1")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if UI_PASS and not _is_local(request) and not request.url.path.startswith(PUBLIC_PATHS):
        hdr = request.headers.get("authorization", "")
        ok = False
        if hdr.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, UI_USER) and secrets.compare_digest(pw, UI_PASS)
            except Exception:
                ok = False
        if not ok:
            return Response("auth required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="support"'})
    return await call_next(request)


# ---------- helpers ----------

async def oc(method: str, path: str, **kw) -> httpx.Response:
    params = kw.pop("params", {}) or {}
    params.setdefault("directory", WORKSPACE)
    try:
        r = await client.request(method, path, params=params, **kw)
    except httpx.HTTPError as e:
        raise HTTPException(503, f"agent engine (opencode) unreachable: {e.__class__.__name__}") from e
    if r.status_code >= 400:
        raise HTTPException(r.status_code, f"opencode {method} {path}: {r.text[:300]}")
    return r


def _targets() -> list[dict]:
    out = []
    if TARGETS_DIR.exists():
        for p in sorted(TARGETS_DIR.glob("*.env")):
            cfg = {"USER": "support-agent", "PORT": "22"}
            for line in p.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1); cfg[k.strip()] = v.strip()
            out.append({"name": p.stem, "host": cfg.get("HOST", "?"), "user": cfg["USER"], "port": cfg["PORT"]})
    return out


def _docs_status() -> dict:
    docs = Path(WORKSPACE) / "docs"
    files = list(docs.rglob("*.md")) if docs.exists() else []
    nbytes = sum(f.stat().st_size for f in files)
    return {"files": len(files), "bytes": nbytes, "tokens_est": nbytes * 2 // 7}


def _normalize_part(p: dict) -> dict | None:
    t = p.get("type")
    if t == "text":
        return {"type": "text", "text": p.get("text", "")}
    if t == "reasoning":
        return {"type": "reasoning", "text": p.get("text", "")}
    if t == "tool":
        st = p.get("state", {}) or {}
        return {"type": "tool", "tool": p.get("tool"), "callID": p.get("callID"), "status": st.get("status"), "input": st.get("input", {}),
                "output": (st.get("output") or st.get("error") or "")[:20000], "title": st.get("title", ""),
                "time": st.get("time") or {}}
    return None


# ---------- API ----------

@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML


@app.get("/sw.js")
async def service_worker():
    """Served from the root so the worker's scope covers the whole app (a /static/ URL would only cover /static/)."""
    return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/ca.crt")
async def ca_cert():
    """Local CA to install on a laptop/phone once, so https://<gx10>:8443 is trusted (mic, PWA)."""
    p = TLS_DIR / "ca.crt"
    if not p.exists():
        raise HTTPException(404, "no local CA generated yet (install/gen-certs.sh)")
    return FileResponse(p, media_type="application/x-x509-ca-cert", filename=f"workbench-{os.uname().nodename}-ca.crt")


@app.get("/api/features")
async def features():
    """What the browser may use: offline STT assets present? which model? HTTPS port?"""
    model_dir = next((d for d in sorted((ASSETS_DIR / "models").glob("*")) if (d / "config.json").exists()), None) \
        if (ASSETS_DIR / "models").exists() else None
    return {
        "stt": bool(model_dir) and (ASSETS_DIR / "vendor" / "transformers.min.js").exists(),
        "stt_model": model_dir.name if model_dir else None,
        "tls": (TLS_DIR / "server.crt").exists(), "tls_port": int(os.environ.get("SUPPORT_UI_TLS_PORT", "8443")),
    }


# Static: PWA shell/worker from the repo, heavy vendor/model assets from the fetched directory.
for sub in ("vendor", "models"):
    d = ASSETS_DIR / sub
    if d.exists():
        app.mount(f"/static/{sub}", StaticFiles(directory=d), name=sub)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/state")
async def state():
    sessions = (await oc("GET", "/session")).json()
    sessions = [s for s in sessions if not s.get("parentID")]
    sessions.sort(key=lambda s: s.get("time", {}).get("updated", 0), reverse=True)
    perms = (await oc("GET", "/permission")).json()
    llm = {"up": False, "models": []}
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{LLM_URL}/v1/models")
            llm = {"up": r.status_code == 200, "models": [m["id"] for m in r.json().get("data", [])]}
    except Exception:
        pass
    profiles = {}
    for prof in ("fast", "deep"):
        profiles[prof] = subprocess.run(["systemctl", "is-active", f"llm@{prof}"], capture_output=True,
                                        text=True).stdout.strip()
    return {
        "sessions": [{"id": s["id"], "title": s.get("title", ""), "updated": s.get("time", {}).get("updated")}
                     for s in sessions[:50]],
        "pending": perms, "llm": llm, "profiles": profiles,
        "targets": _targets(), "docs": _docs_status(),
    }


@app.post("/api/session")
async def create_session(body: dict):
    title = (body or {}).get("title") or "Support session"
    r = await oc("POST", "/session", json={"title": title})
    return r.json()


@app.get("/api/session/{sid}")
async def get_session(sid: str):
    msgs = (await oc("GET", f"/session/{sid}/message")).json()
    status = {}
    try:
        status = (await oc("GET", "/session/status")).json()
    except HTTPException:
        pass
    out = []
    for m in msgs:
        info, parts = m["info"], m["parts"]
        norm = [x for x in (_normalize_part(p) for p in parts) if x]
        if not norm:
            continue
        out.append({"id": info["id"], "role": info["role"], "time": info.get("time", {}).get("created"),
                    "error": (info.get("error") or {}).get("data", {}).get("message") if info.get("error") else None,
                    "parts": norm})
    perms = [p for p in (await oc("GET", "/permission")).json() if p.get("sessionID") == sid]
    busy = bool(status.get(sid)) and status.get(sid, {}).get("type") not in (None, "idle")
    return {"messages": out, "pending": perms, "busy": busy}


@app.post("/api/session/{sid}/prompt")
async def prompt(sid: str, body: dict):
    text = (body or {}).get("text", "").strip()
    if not text:
        raise HTTPException(400, "empty prompt")
    await oc("POST", f"/session/{sid}/prompt_async", json={"parts": [{"type": "text", "text": text}]})
    return {"ok": True}


@app.post("/api/session/{sid}/abort")
async def abort(sid: str):
    await oc("POST", f"/session/{sid}/abort")
    return {"ok": True}


@app.post("/api/session/{sid}/permission/{pid}")
async def permission(sid: str, pid: str, body: dict):
    resp = (body or {}).get("response")
    if resp not in ("once", "always", "reject"):
        raise HTTPException(400, "response must be once|always|reject")
    await oc("POST", f"/session/{sid}/permissions/{pid}", json={"response": resp})
    return {"ok": True}


@app.get("/api/audit")
async def audit(n: int = 40):
    if not AUDIT_LOG.exists():
        return []
    lines = AUDIT_LOG.read_text().splitlines()[-n:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    return list(reversed(out))


@app.get("/api/health")
async def health():
    try:
        await oc("GET", "/session/status")
        return JSONResponse({"ok": True})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
