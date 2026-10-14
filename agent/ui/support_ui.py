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
CLASSIC_HTML = (Path(__file__).parent / "classic.html").read_text()
PUBLIC_PATHS = ("/ca.crt", "/sw.js", "/static/manifest.json", "/static/icons/")   # no login needed
MCP_DIR = Path(os.environ.get("SUPPORT_MCP_DIR", str(Path(__file__).parent.parent / "mcp")))
SUPPORT_CTL = os.environ.get("SUPPORT_CTL", "/usr/local/bin/support-ctl")
STACK_DIR = Path(os.environ.get("SUPPORT_STACK_DIR", "/opt/support-stack"))


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
    for prof in ("fast", "deep"):
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
        "pending": perms, "llm": llm, "profiles": profiles, "models": _model_names(),
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
    for prof in ("fast", "deep"):
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
    if not text:
        raise HTTPException(400, "empty prompt")
    payload: dict = {"parts": [{"type": "text", "text": text}]}
    mid = await active_model()
    if mid:
        payload["model"] = {"providerID": "spark", "modelID": mid}
    else:
        raise HTTPException(503, "LLM is not serving any model (support-ctl status)")
    await oc("POST", f"/session/{sid}/prompt_async", json=payload)
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
    if prof not in ("fast", "deep"):
        raise HTTPException(400, "profile must be fast|deep")
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


@app.get("/api/health")
async def health():
    try:
        await oc("GET", "/session/status")
        return JSONResponse({"ok": True})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
