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

import asyncio
import base64
import json
import os
import re
import secrets
import subprocess
import time
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
CLASSIC_HTML = (Path(__file__).parent / "classic.html").read_text()
PUBLIC_PATHS = ("/ca.crt", "/sw.js", "/favicon.ico", "/static/manifest.json", "/static/icons/")   # no login needed
MCP_DIR = Path(os.environ.get("SUPPORT_MCP_DIR", str(Path(__file__).parent.parent / "mcp")))
SUPPORT_CTL = os.environ.get("SUPPORT_CTL", "/usr/local/bin/support-ctl")
STACK_DIR = Path(os.environ.get("SUPPORT_STACK_DIR", "/opt/support-stack"))
LIBRARY_DIR = Path(os.environ.get("LIBRARY_DIR", "/opt/library"))
MEDIA_DIR = Path(os.environ.get("SUPPORT_MEDIA_DIR", "/var/lib/support/media"))
NOTES_DIR = Path(os.environ.get("SUPPORT_NOTES_DIR", str(Path(WORKSPACE) / "docs" / "devices")))
LOOP_RESET = Path(os.environ.get("SUPPORT_LOOP_RESET", "/var/lib/support/state/loop-reset"))
VISION_MODELS = {"fast"}            # profiles whose model sees images (opencode.json: modalities)


def _library():
    """library_core (agent/mcp), imported in-process: the Library tab searches the same index as the agent."""
    import importlib
    import sys
    if str(MCP_DIR) not in sys.path:
        sys.path.insert(0, str(MCP_DIR))
    os.environ.setdefault("LIBRARY_DIR", str(LIBRARY_DIR))
    return importlib.import_module("library_core")


def _target_host():
    """The MCP server module, imported in-process so the UI can show a device passport / operator console
    without going through the agent. Same allowlists, same audit log."""
    import importlib
    import sys
    if str(MCP_DIR) not in sys.path:
        sys.path.insert(0, str(MCP_DIR))
    return importlib.import_module("target_host")

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


# Action classes by tool name (mirrors agent/mcp/target_host.py). "always" is refused for ACTUATION tools.
ACTUATION_TOOLS = {"device_actuate", "device_recover", "reboot_and_wait"}
CONFIG_TOOLS = {"run_change", "write_file", "device_act", "workbench_run"}


def _tool_class(tool: str) -> str:
    t = (tool or "").replace("target-host_", "")
    return "actuation" if t in ACTUATION_TOOLS else "config" if t in CONFIG_TOOLS else "read"


def _targets() -> list[dict]:
    out = []
    if TARGETS_DIR.exists():
        for p in sorted(TARGETS_DIR.glob("*.env")):
            cfg = {}
            for line in p.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1); cfg[k.strip()] = v.strip().strip('"')
            t = (cfg.get("TYPE") or "ssh").lower()
            if t == "serial":
                where = f"{cfg.get('PORT', '/dev/ttyUSB0')} @ {cfg.get('BAUD', 115200)}"
            elif t == "can":
                where = f"{cfg.get('IFACE', 'can0')} @ {cfg.get('BITRATE', 500000)}"
            elif t == "modbus":
                port = cfg.get("PORT", "502")
                where = f"{port} unit {cfg.get('UNIT', 1)}" if port.startswith("/dev/") else f"{cfg.get('HOST', '?')}:{port} unit {cfg.get('UNIT', 1)}"
            elif t == "http":
                where = cfg.get("URL", "?")
            else:
                where = f"{cfg.get('USER', 'support-agent')}@{cfg.get('HOST', 'auto')}" + (f" ({cfg['IP_LAST']})" if cfg.get("HOST", "auto") == "auto" and cfg.get("IP_LAST") else "")
            out.append({"name": p.stem, "type": t, "where": where,
                        "host": cfg.get("HOST", "?"), "user": cfg.get("USER", "support-agent"), "port": cfg.get("PORT", "22")})
    return out


def _docs_status() -> dict:
    docs = Path(WORKSPACE) / "docs"
    files = list(docs.rglob("*.md")) if docs.exists() else []
    nbytes = sum(f.stat().st_size for f in files)
    return {"files": len(files), "bytes": nbytes, "tokens_est": nbytes * 2 // 7}


def _err_text(err) -> str | None:
    if not err:
        return None
    if isinstance(err, dict):
        data = err.get("data") or {}
        msg = data.get("message") if isinstance(data, dict) else None
        return msg or err.get("message") or err.get("name") or json.dumps(err)[:200]
    return str(err)[:200]


def _normalize_part(p: dict) -> dict | None:
    t = p.get("type")
    if t == "text":
        return {"type": "text", "text": p.get("text", "")}
    if t == "reasoning":
        return {"type": "reasoning", "text": p.get("text", "")}
    if t == "file":
        fn, mime = p.get("filename") or "", p.get("mime") or ""
        m = re.fullmatch(r"media/([\w-]{4,80})/([\w.-]{1,120})", fn)
        if mime.startswith("image/") and m:
            return {"type": "image", "src": f"/api/media/{m.group(1)}/{m.group(2)}", "name": m.group(2)}
        return {"type": "file", "name": fn or "file", "mime": mime}
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


@app.get("/classic", response_class=HTMLResponse)
async def classic():
    """The pre-v2 single-column UI, kept as a fallback."""
    return CLASSIC_HTML


@app.get("/sw.js")
async def service_worker():
    """Served from the root so the worker's scope covers the whole app (a /static/ URL would only cover /static/)."""
    return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico")
async def favicon():
    """Browsers and bookmarks ask for /favicon.ico regardless of <link rel=icon>."""
    return FileResponse(STATIC_DIR / "icons" / "favicon.ico", media_type="image/x-icon", headers={"Cache-Control": "max-age=86400"})


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
        "mermaid": (ASSETS_DIR / "vendor" / "mermaid.min.js").exists(),
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
    for prof in ("fast", "deep", "nemotron"):
        profiles[prof] = subprocess.run(["systemctl", "is-active", f"llm@{prof}"], capture_output=True,
                                        text=True).stdout.strip()
    status = {}
    try:
        status = (await oc("GET", "/session/status")).json()
    except HTTPException:
        pass
    return {
        "sessions": [{"id": s["id"], "title": s.get("title", ""), "updated": s.get("time", {}).get("updated"),
                      "busy": bool(status.get(s["id"])) and (status.get(s["id"]) or {}).get("type") not in (None, "idle")}
                     for s in sessions[:80]],
        "pending": perms, "llm": llm, "profiles": profiles, "models": _model_names(), "self_until": _self_until(),
        "targets": _targets(), "docs": _docs_status(), "box": _box_status(),
        "classes": {"actuation": sorted(ACTUATION_TOOLS), "config": sorted(CONFIG_TOOLS)},
    }


_box_cache: dict = {"at": 0.0, "v": {}}


def _box_status() -> dict:
    """Memory and RJ45 link of the Workbench box for the status strip (cached 5 s)."""
    import time
    if time.time() - _box_cache["at"] < 5:
        return _box_cache["v"]
    v: dict = {"host": os.uname().nodename}
    try:
        mi = {ln.split(":")[0]: int(ln.split()[1]) for ln in Path("/proc/meminfo").read_text().splitlines() if ":" in ln}
        v["mem_total_gib"] = round(mi["MemTotal"] / 1048576, 1)
        v["mem_used_gib"] = round((mi["MemTotal"] - mi["MemAvailable"]) / 1048576, 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        rj = subprocess.run(["sh", "-c", "ip -o link | awk -F': ' '$2 ~ /^en/ && $2 !~ /docker|veth|br-/ {print $2}' | head -1"],
                            capture_output=True, text=True, timeout=3).stdout.strip()
        if rj:
            st = subprocess.run(["nmcli", "-g", "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS", "device", "show", rj],
                                capture_output=True, text=True, timeout=3).stdout.strip().splitlines()
            v["link"] = {"iface": rj, "state": st[0] if st else "", "profile": st[1] if len(st) > 1 else "",
                         "addr": st[2] if len(st) > 2 else ""}
    except Exception:  # noqa: BLE001
        pass
    _box_cache.update(at=time.time(), v=v)
    return v


def _model_names() -> dict:
    """Human names of the profiles from stack env files (MODEL_PATH basename)."""
    out = {}
    for prof in ("fast", "deep", "nemotron"):
        env = STACK_DIR / "env" / f"{prof}.env"
        name = ""
        if env.exists():
            for ln in env.read_text().splitlines():
                if ln.startswith("MODEL_PATH="):
                    name = ln.split("=", 1)[1].strip().rsplit("/", 1)[-1]
        out[prof] = name
    return out


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
        if not norm and not info.get("error"):
            continue                      # a failed turn has no parts but must still show its error
        out.append({"id": info["id"], "role": info["role"], "time": info.get("time", {}).get("created"),
                    "error": _err_text(info.get("error")),
                    "parts": norm})
    perms = [p for p in (await oc("GET", "/permission")).json() if p.get("sessionID") == sid]
    questions = []
    try:
        questions = [q for q in (await oc("GET", "/question")).json() if q.get("sessionID") == sid]
    except HTTPException:
        pass
    busy = bool(status.get(sid)) and status.get(sid, {}).get("type") not in (None, "idle")
    return {"messages": out, "pending": perms, "questions": questions, "busy": busy}


_active_model: dict = {"at": 0.0, "id": None}


async def active_model() -> str | None:
    """Which profile vLLM is serving right now (fast/deep). Sessions default to spark/fast in opencode.json;
    after `support-ctl model deep` every prompt must target the model that actually exists."""
    import time
    if time.time() - _active_model["at"] < 10:
        return _active_model["id"]
    mid = None
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{LLM_URL}/v1/models")
            ids = [m["id"] for m in r.json().get("data", [])]
            mid = next((i for i in ids if i in ("fast", "deep")), ids[0] if ids else None)
    except Exception:
        pass
    _active_model.update(at=time.time(), id=mid)
    return mid


@app.post("/api/session/{sid}/prompt")
async def prompt(sid: str, body: dict):
    text = (body or {}).get("text", "").strip()
    images = [str(x) for x in (body or {}).get("images", [])][:8]
    if not text and not images:
        raise HTTPException(400, "empty prompt")
    mid = await active_model()
    if not mid:
        raise HTTPException(503, "LLM is not serving any model (support-ctl status)")
    if images and mid not in VISION_MODELS:
        raise HTTPException(409, f"модель «{mid}» не видит изображения — переключитесь на fast или отправьте без фото")
    payload: dict = {"parts": [{"type": "text", "text": text or "Посмотри на фото."}], "model": {"providerID": "spark", "modelID": mid}}
    for name in images:
        f = _media_path(sid, name)
        mime = "image/png" if f.suffix == ".png" else "image/webp" if f.suffix == ".webp" else "image/jpeg"
        payload["parts"].append({"type": "file", "mime": mime, "filename": f"media/{sid}/{name}",
                                 "url": f"data:{mime};base64," + base64.b64encode(f.read_bytes()).decode()})
    await oc("POST", f"/session/{sid}/prompt_async", json=payload)
    try:   # the operator stepped in: the loop guard in target-host starts counting afresh
        LOOP_RESET.parent.mkdir(parents=True, exist_ok=True)
        LOOP_RESET.touch()
    except OSError:
        pass
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
    if resp == "always":
        # ACTUATION class is approved one call at a time, whatever the browser sent (PLAN.md §4 phase A).
        pending = [p for p in (await oc("GET", "/permission")).json() if p.get("id") == pid]
        for p in pending:
            tool = (p.get("tool") or {}).get("name") if isinstance(p.get("tool"), dict) else None
            tool = tool or (p.get("metadata") or {}).get("tool") or p.get("permission") or ""
            if _tool_class(tool) == "actuation":
                raise HTTPException(409, "actuation is approved one call at a time; use 'once'")
    await oc("POST", f"/session/{sid}/permissions/{pid}", json={"response": resp})
    return {"ok": True}


@app.post("/api/session/{sid}/question/{qid}")
async def question_reply(sid: str, qid: str, body: dict):
    """Answer (or reject) a question the agent asked through its `question` tool; the turn is blocked until then."""
    if (body or {}).get("reject"):
        await oc("POST", f"/question/{qid}/reject")
        return {"ok": True}
    answers = (body or {}).get("answers")
    if not isinstance(answers, list):
        raise HTTPException(400, "answers must be a list of lists of labels")
    await oc("POST", f"/question/{qid}/reply", json={"answers": answers})
    return {"ok": True}


@app.post("/api/model")
async def switch_model(body: dict):
    """Start llm@fast or llm@deep (Conflicts= in the unit stops the other). Needs the sudoers rule from deploy.sh."""
    prof = (body or {}).get("profile")
    if prof not in ("fast", "deep", "nemotron"):
        raise HTTPException(400, "profile must be fast|deep|nemotron")
    env = Path(os.environ.get("SUPPORT_STACK_DIR", "/opt/support-stack")) / "env" / f"{prof}.env"
    mp = next((ln.split("=", 1)[1].strip() for ln in env.read_text().splitlines() if ln.startswith("MODEL_PATH=")), "") if env.exists() else ""
    local = Path("/opt" + mp) if mp.startswith("/models/") else None
    if not local or not (local / "config.json").exists():
        raise HTTPException(409, f"веса профиля {prof} не скачаны ({local}); install/fetch-models.sh {prof}")
    r = subprocess.run(["sudo", "-n", "systemctl", "start", f"llm@{prof}"], capture_output=True, text=True)
    if r.returncode != 0:
        raise HTTPException(500, f"cannot start llm@{prof}: {(r.stderr or r.stdout).strip()[:200]}")
    _active_model.update(at=0.0)
    return {"ok": True, "profile": prof}


# ---------- device passport / operator console (in-process MCP module, same allowlists + audit) ----------

@app.get("/api/device/{name}")
async def device_info(name: str):
    th = _target_host()
    try:
        cfg = th._load_target(name)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    import connectors
    return {"name": name, "type": cfg["TYPE"], "where": connectors.describe(cfg),
            "cfg": {k: v for k, v in cfg.items() if k not in ("KEY", "NAME")}}


@app.post("/api/device/{name}/facts")
async def device_facts(name: str, body: dict | None = None):
    """Passport: collect_facts for ssh devices, a default observe for the rest. Runs as the operator (audited)."""
    th = _target_host()
    try:
        cfg = th._load_target(name)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    import time
    t0 = time.time()
    if cfg["TYPE"] == "ssh":
        text = await th.collect_facts(name)
    else:
        q = (body or {}).get("query") or {"serial": "", "can": "dump", "modbus": "holding 0 10", "http": "GET /"}[cfg["TYPE"]]
        text = await th.observe(name, q, float((body or {}).get("seconds") or 2))
    return {"name": name, "type": cfg["TYPE"], "text": text, "seconds": round(time.time() - t0, 1),
            "sections": _parse_facts(text) if cfg["TYPE"] == "ssh" else {}}


def _parse_facts(text: str) -> dict:
    """Split collect_facts output ('### title' blocks) into {title: body}."""
    out, cur = {}, None
    for ln in text.splitlines():
        if ln.startswith("### "):
            cur = ln[4:].strip(); out[cur] = []
        elif cur is not None:
            out[cur].append(ln)
    return {k: "\n".join(v).strip() for k, v in out.items()}


@app.post("/api/device/{name}/run")
async def device_run(name: str, body: dict):
    """Operator console: a READ-class command on the device (run_readonly allowlist / observe). Audited as operator."""
    cmd = (body or {}).get("cmd", "").strip()
    if not cmd:
        raise HTTPException(400, "empty command")
    th = _target_host()
    try:
        cfg = th._load_target(name)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    import time
    t0 = time.time()
    text = await (th.run_readonly(cmd, name) if cfg["TYPE"] == "ssh" else th.observe(name, cmd, float((body or {}).get("seconds") or 3)))
    th._audit({"tool": "console", "target": name, "cmd": cmd, "by": "operator", "rc": 0 if not text.startswith(("REJECTED", "ERROR")) else 1})
    return {"text": text, "seconds": round(time.time() - t0, 1), "rejected": text.startswith("REJECTED")}


# ---------- the Workbench box itself: closed to agent changes unless the operator opens it for a while ----------

SELF_UNLOCK = Path(os.environ.get("SUPPORT_SELF_UNLOCK", "/var/lib/support/state/self-unlock.json"))


def _self_until() -> float:
    try:
        until = float(json.loads(SELF_UNLOCK.read_text()).get("until", 0))
    except (OSError, ValueError):
        return 0.0
    return until if until > time.time() else 0.0


def _audit_operator(event: dict) -> None:
    try:
        _target_host()._audit({**event, "by": "operator"})
    except Exception:  # noqa: BLE001 — the audit log must not break the operator's controls
        pass


@app.get("/api/self")
async def self_state():
    return {"until": _self_until()}


@app.post("/api/self")
async def self_unlock(body: dict, request: Request):
    """Operator opens the Workbench box (target `self`) to agent changes for 1–240 minutes. Each change still needs
    approval; the auto-approve mode never approves changes on self."""
    minutes = int((body or {}).get("minutes") or 30)
    if not 1 <= minutes <= 240:
        raise HTTPException(400, "minutes: 1–240")
    until = time.time() + minutes * 60
    SELF_UNLOCK.parent.mkdir(parents=True, exist_ok=True)
    tmp = SELF_UNLOCK.with_suffix(".tmp")
    tmp.write_text(json.dumps({"until": until, "by": "operator", "via": "ui", "client": request.client.host if request.client else ""}))
    tmp.replace(SELF_UNLOCK)
    _audit_operator({"tool": "self_unlock", "minutes": minutes})
    return {"until": until}


@app.delete("/api/self")
async def self_lock():
    SELF_UNLOCK.unlink(missing_ok=True)
    _audit_operator({"tool": "self_lock"})
    return {"until": 0}


@app.post("/api/device/onboard")
async def device_onboard(body: dict):
    """Operator connects a Linux host: name + user@host[:port] + that user's password (typed in the UI, used once by
    `support-ctl target add --password-stdin`, never stored or shown to the agent). Installs the agent key there."""
    name = str((body or {}).get("name", "")).strip()
    userhost = str((body or {}).get("userhost", "")).strip()
    password = str((body or {}).get("password", ""))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", name):
        raise HTTPException(400, "имя: латиница, цифры, - и _, до 32 символов")
    if not re.fullmatch(r"[A-Za-z0-9._-]+@[A-Za-z0-9._:-]+", userhost):
        raise HTTPException(400, "адрес в виде user@host или user@host:port")
    if not password or "\n" in password:
        raise HTTPException(400, "нужен пароль")
    try:
        r = await asyncio.to_thread(subprocess.run, [SUPPORT_CTL, "target", "add", name, userhost, "--password-stdin"],
                                    input=password + "\n", capture_output=True, text=True, timeout=300)
        out, rc = (r.stdout + r.stderr).replace(password, "***"), r.returncode
    except subprocess.TimeoutExpired:
        out, rc = "timeout: host did not answer in 5 minutes", 124
    finally:
        password = ""
    try:
        _target_host()._audit({"tool": "onboard", "by": "operator", "target": name, "userhost": userhost, "rc": rc})
    except Exception:  # noqa: BLE001
        pass
    return {"ok": rc == 0, "rc": rc, "text": out[-4000:]}


@app.get("/api/device/{name}/notes")
async def device_notes(name: str):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", name):
        raise HTTPException(400, "bad name")
    p = NOTES_DIR / f"{name}.md"
    return {"name": name, "text": p.read_text() if p.exists() else "", "path": str(p),
            "updated": p.stat().st_mtime if p.exists() else None}


@app.post("/api/scan")
async def scan(body: dict | None = None):
    what = (body or {}).get("what") or "all"
    if what not in ("all", "usb", "serial", "can", "net", "mdns"):
        raise HTTPException(400, "what must be all|usb|serial|can|net|mdns")
    try:
        r = subprocess.run([SUPPORT_CTL, "scan", what], capture_output=True, text=True, timeout=120)
        text = r.stdout + r.stderr
    except Exception as e:  # noqa: BLE001
        text = f"ERROR: {e}"
    sections, cur = {}, None
    for ln in text.splitlines():
        if ln.startswith("== "):
            cur = ln[3:].strip(); sections[cur] = []
        elif cur is not None:
            sections[cur].append(ln.rstrip())
    return {"text": text, "sections": {k: "\n".join(v).strip() for k, v in sections.items()}}


@app.get("/api/device/{name}/report")
async def device_report(name: str):
    """Device passport as Markdown (facts + scan), for the session report."""
    import datetime as dt
    th = _target_host()
    try:
        cfg = th._load_target(name)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    import connectors
    facts = await th.collect_facts(name) if cfg["TYPE"] == "ssh" else await th.observe(name, "", 2)
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    md = [f"# Паспорт устройства {name}", "", f"{connectors.describe(cfg)} · собрано {stamp} с {os.uname().nodename}", "", "```", facts, "```", ""]
    return Response("\n".join(md), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="passport-{name}-{dt.datetime.now().strftime("%Y%m%d-%H%M")}.md"'})


@app.get("/api/audit/export")
async def audit_export(format: str = "jsonl"):
    import csv
    import datetime as dt
    import io
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    if not AUDIT_LOG.exists():
        raise HTTPException(404, "no audit log yet")
    if format == "csv":
        rows = []
        for ln in AUDIT_LOG.read_text().splitlines():
            try:
                rows.append(json.loads(ln))
            except json.JSONDecodeError:
                pass
        cols = ["ts", "target", "tool", "cmd", "path", "reason", "rc", "rejected", "mode"]
        buf = io.StringIO(); w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore"); w.writeheader(); w.writerows(rows)
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="audit-{os.uname().nodename}-{stamp}.csv"'})
    return FileResponse(AUDIT_LOG, media_type="application/x-ndjson", filename=f"audit-{os.uname().nodename}-{stamp}.jsonl")


@app.get("/api/session/{sid}/export")
async def session_export(sid: str):
    """Whole session as Markdown: messages, tool calls with their output, errors."""
    import datetime as dt
    info = (await oc("GET", f"/session/{sid}")).json()
    msgs = (await oc("GET", f"/session/{sid}/message")).json()
    def ts(ms):
        return dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if ms else ""
    lines = [f"# {info.get('title') or sid}", "", f"Сессия `{sid}`, создана {ts(info.get('time', {}).get('created'))}, "
             f"экспорт {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} с {os.uname().nodename}", ""]
    for m in msgs:
        i, parts = m["info"], m["parts"]
        who = "Оператор" if i["role"] == "user" else f"Агент ({i.get('modelID') or ''})"
        lines += [f"## {who} · {ts(i.get('time', {}).get('created'))}", ""]
        for p in parts:
            t = p.get("type")
            if t == "text":
                lines += [p.get("text", ""), ""]
            elif t == "file":
                lines += [f"*вложение: {p.get('filename') or p.get('mime')}*", ""]
            elif t == "reasoning" and p.get("text"):
                lines += ["<details><summary>ход мыслей</summary>", "", p["text"], "", "</details>", ""]
            elif t == "tool":
                st = p.get("state") or {}
                inp = st.get("input") or {}
                lines += [f"**{p.get('tool')}** · {st.get('status')}" + (f" · {inp.get('target')}" if inp.get("target") else ""), ""]
                if inp:
                    lines += ["```json", json.dumps(inp, ensure_ascii=False, indent=1)[:4000], "```", ""]
                out = st.get("output") or st.get("error")
                if out:
                    lines += ["```", str(out)[:20000], "```", ""]
        err = _err_text(i.get("error"))
        if err:
            lines += [f"> Ошибка: {err}", ""]
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    return Response("\n".join(lines), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="session-{sid[-6:]}-{stamp}.md"'})


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


# ---------- library (roadmap phase C): same index as the agent's `library` MCP server ----------

@app.get("/api/library")
async def library_overview(kind: str = ""):
    lib = _library()
    return {"stats": await asyncio.to_thread(lib.stats), "docs": await asyncio.to_thread(lib.list_docs, kind)}


@app.get("/api/library/search")
async def library_search(q: str, kind: str = "", k: int = 12):
    lib = _library()
    hits = await asyncio.to_thread(lib.search, q, max(1, min(k, 30)), kind if kind in lib.KINDS else "")
    return [{**h, "text": h["text"][:1500]} for h in hits]


@app.get("/api/library/doc/{doc_id}")
async def library_doc(doc_id: int, page: int = 0, chunk: int = -1, around: int = 1):
    try:
        return await asyncio.to_thread(_library().read, doc_id, page, chunk, around, 40000)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/library/file/{doc_id}")
async def library_file(doc_id: int):
    lib = _library()
    doc = next((d for d in lib.list_docs() if d["id"] == doc_id), None)
    p = (LIBRARY_DIR / doc["path"]) if doc else None
    if not p or not p.exists():
        raise HTTPException(404, "file not found")
    return FileResponse(p, filename=p.name, content_disposition_type="inline")


@app.post("/api/library/upload")
async def library_upload(request: Request, name: str, kind: str = "manual"):
    """Raw request body = the file. Copied to LIBRARY_DIR/docs/<kind>/ and indexed (embeddings when installed)."""
    lib = _library()
    if kind not in lib.KINDS:
        raise HTTPException(400, f"kind: {', '.join(lib.KINDS)}")
    name = Path(name).name
    if Path(name).suffix.lower() not in lib.EXTS:
        raise HTTPException(415, f"формат не поддерживается: {', '.join(sorted(lib.EXTS))}")
    data = await request.body()
    if not data or len(data) > 300 * 2**20:
        raise HTTPException(413, "пустой файл или больше 300 МБ")
    tmp = LIBRARY_DIR / "incoming"
    tmp.mkdir(parents=True, exist_ok=True)
    src = tmp / re.sub(r"[^\w.\-() ]+", "_", name)
    src.write_bytes(data)
    try:
        res = await asyncio.to_thread(lib.add, src, kind)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(422, f"не удалось проиндексировать: {e}")
    finally:
        src.unlink(missing_ok=True)
    return res


@app.delete("/api/library/{doc_id}")
async def library_delete(doc_id: int):
    if not await asyncio.to_thread(_library().remove, doc_id):
        raise HTTPException(404, "no such document")
    return {"ok": True}


# ---------- media (roadmap phase D): photos per session, sent to the vision model as file parts ----------

IMG_MAGIC = {b"\xff\xd8\xff": ".jpg", b"\x89PNG": ".png", b"RIFF": ".webp"}


def _media_path(sid: str, name: str = "") -> Path:
    if not re.fullmatch(r"[\w-]{4,80}", sid) or (name and not re.fullmatch(r"[\w-]{1,100}\.(jpg|png|webp)", name)):
        raise HTTPException(400, "bad media id")
    p = MEDIA_DIR / sid / name if name else MEDIA_DIR / sid
    if name and not p.exists():
        raise HTTPException(404, "no such photo")
    return p


@app.get("/api/media/{sid}")
async def media_list(sid: str):
    d = _media_path(sid)
    files = sorted(d.glob("*.*"), key=lambda f: f.stat().st_mtime) if d.exists() else []
    return [{"name": f.name, "size": f.stat().st_size, "time": f.stat().st_mtime, "src": f"/api/media/{sid}/{f.name}"}
            for f in files if f.suffix in (".jpg", ".png", ".webp")]


@app.post("/api/media/{sid}")
async def media_upload(sid: str, request: Request):
    """Raw body = an image (the browser already scaled it down to ≤1600 px JPEG)."""
    data = await request.body()
    if len(data) > 20 * 2**20:
        raise HTTPException(413, "фото больше 20 МБ")
    ext = next((e for m, e in IMG_MAGIC.items() if data.startswith(m)), None)
    if not ext:
        raise HTTPException(415, "нужен JPEG, PNG или WebP")
    d = _media_path(sid)
    d.mkdir(parents=True, exist_ok=True)
    name = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3) + ext
    (d / name).write_bytes(data)
    return {"name": name, "src": f"/api/media/{sid}/{name}", "size": len(data)}


@app.get("/api/media/{sid}/{name}")
async def media_file(sid: str, name: str):
    return FileResponse(_media_path(sid, name), headers={"Cache-Control": "max-age=86400"})


@app.delete("/api/media/{sid}/{name}")
async def media_delete(sid: str, name: str):
    _media_path(sid, name).unlink()
    return {"ok": True}


@app.get("/api/health")
async def health():
    try:
        await oc("GET", "/session/status")
        return JSONResponse({"ok": True})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
