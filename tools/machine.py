#!/usr/bin/env python3
"""MCP server `machine`: a research workspace on this box — shell commands, background jobs, files, downloads.

On the box this server runs as the unprivileged user `researcher` (install.sh: own uid, no sudo, no docker group,
iptables lets it reach only the internet, DNS and the LLM on 127.0.0.1:8000). Everything it starts inherits
that user and no_new_privs, so a command cannot reach the operator's home, the chats database, sudo or the other
local services. Without that user (local development) it runs as whoever started the app.

Layout (RESEARCH_HOME, default /var/lib/research/runner = researcher's home):
    workspace/  RESEARCH_WORKSPACE, default /var/lib/research/workspace — projects, data, scripts, results;
                the chat links files here as /ws/<path>
    runner/venv Python with the scientific stack (numpy, pandas, xarray, matplotlib, …); `pip install` works
    runner/jobs logs and state of background jobs
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).parent))
from common import UA, html_to_text, page, pdf_to_text  # noqa: E402

WS = Path(os.environ.get("RESEARCH_WORKSPACE", "/var/lib/research/workspace")).resolve()
HOME = Path(os.environ.get("RESEARCH_HOME", "/var/lib/research/runner")).resolve()
VENV = HOME / "venv"
JOBS = HOME / "jobs"
OUT_CHARS = 12_000          # command output returned to the model (head + tail)
RUN_TIMEOUT_MAX = 900       # seconds; longer work goes to job_start
DOWNLOAD_MAX_MB = 20_480
SKIP = {".git", "__pycache__", ".ipynb_checkpoints", ".cache", "node_modules", ".venv", "venv"}
TEXT_LIMIT = 50 * 2**20     # read_file refuses to load bigger files whole; use run (head, grep, pandas)

mcp = FastMCP("machine")
PROCS: dict[int, subprocess.Popen] = {}  # jobs started by this server: polled so finished ones do not linger as zombies


def _no_new_privs():
    """Children can never gain privileges (sudo, setuid binaries), even if the user had any."""
    try:
        ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0)  # PR_SET_NO_NEW_PRIVS
    except Exception:
        pass


def _fence_ok() -> bool:
    """As the sandbox user, local services must be unreachable (runner-net); otherwise refuse to serve tools.
    Probes ports that are up before the chat itself listens: sshd and SearXNG."""
    import pwd
    import socket
    if pwd.getpwuid(os.getuid()).pw_name != os.environ.get("RESEARCH_FENCE_USER", "researcher"):
        return True  # development: no sandbox user, no fence to check
    for port in (22, 8888):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
            return False
        except OSError:
            pass
    return True


def _setup():
    if not _fence_ok():
        sys.exit("machine: network fence is down (local services reachable); run: sudo systemctl start research-runner-net")
    WS.mkdir(parents=True, exist_ok=True)
    JOBS.mkdir(parents=True, exist_ok=True)
    os.umask(0o002)  # group-readable results: the chat (operator user, group researcher) serves them as /ws/...
    _no_new_privs()


def resolve(path: str, must_exist: bool = False) -> Path:
    """Workspace path: relative to the workspace, absolute inside it, or a chat link /ws/... Nothing outside."""
    if path and (path == "/ws" or path.startswith("/ws/")):
        path = path[4:] or "."
    p = Path(path or ".")
    p = (p if p.is_absolute() else WS / p).resolve()
    if p != WS and WS not in p.parents:
        raise ValueError(f"{path}: outside the workspace ({WS}); only workspace paths are allowed")
    if must_exist and not p.exists():
        raise FileNotFoundError(f"{rel(p)} does not exist")
    return p


def rel(p: Path) -> str:
    return str(p.relative_to(WS)) if p != WS else "."


def env() -> dict:
    e = {k: v for k, v in os.environ.items() if k in ("LANG", "LC_ALL", "TERM", "TZ")}
    path = f"{VENV}/bin:" if (VENV / "bin").is_dir() else ""
    e.update({"HOME": str(HOME), "USER": os.environ.get("USER", "researcher"), "WORKSPACE": str(WS),
              "PATH": path + "/usr/local/bin:/usr/bin:/bin", "MPLBACKEND": "Agg", "PYTHONUNBUFFERED": "1",
              "PIP_DISABLE_PIP_VERSION_CHECK": "1", "LLM_URL": "http://127.0.0.1:8000", "LANG": e.get("LANG", "C.UTF-8")})
    if (VENV / "bin").is_dir():
        e["VIRTUAL_ENV"] = str(VENV)
    return e


def clip(text: str, limit: int = OUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head, tail = text[:limit // 3], text[-(limit * 2 // 3):]
    return f"{head}\n\n[... {len(text) - len(head) - len(tail)} chars cut ...]\n\n{tail}"


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return str(n)


# ---------- shell ----------

@mcp.tool()
def run(command: str, cwd: str = "", timeout: int = 120) -> str:
    """Run a bash command in the research workspace and return exit code + output (stdout and stderr merged).
    For scripts, data processing, git clone, pip install, quick checks (ls, head, grep, wc, python -c ...).
    cwd: workspace-relative directory (default: workspace root, also in $WORKSPACE). Inside scripts use paths
    relative to the cwd or $WORKSPACE — /ws/... is only the browser link, not a directory. timeout: seconds, max 900 — use job_start for
    anything longer (training, big downloads, long simulations). Python is a venv with numpy, pandas, scipy,
    xarray, matplotlib (Agg backend: save plots to files), scikit-learn, statsmodels; `pip install X` adds more.
    The local LLM's OpenAI API is at $LLM_URL for batch processing from scripts."""
    wd = resolve(cwd, must_exist=True)
    timeout = max(1, min(int(timeout or 120), RUN_TIMEOUT_MAX))
    t0 = time.time()
    proc = subprocess.Popen(["bash", "-c", command], cwd=wd, env=env(), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
        status = f"exit {proc.returncode}"
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        status = f"KILLED after {timeout}s timeout (use job_start for long work)"
    text = out.decode("utf-8", "replace")
    return f"[{status} · {time.time() - t0:.1f}s · cwd {wd}]\n{clip(text) or '[no output]'}"


# ---------- background jobs ----------

def _job(job_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{8}", job_id or ""):
        raise ValueError("bad job id")
    f = JOBS / f"{job_id}.json"
    if not f.exists():
        raise FileNotFoundError(f"no job {job_id}")
    j = json.loads(f.read_text())
    rc = JOBS / f"{job_id}.rc"
    if rc.exists():
        j["state"], j["exit"] = "finished", rc.read_text().strip()
        j["ended"] = rc.stat().st_mtime
    else:
        proc = PROCS.get(j["pid"])
        try:
            os.kill(j["pid"], 0)
            alive = proc is None or proc.poll() is None
        except ProcessLookupError:
            alive = False
        j["state"] = "running" if alive else "stopped"  # killed (job_stop, reboot or service restart) before it could write its exit code
    return j


@mcp.tool()
def job_start(command: str, cwd: str = "", name: str = "") -> str:
    """Start a long-running bash command in the background (no time limit) and return its job id at once.
    Output goes to a log file; check progress with job_status. Jobs stop if the box or the chat service restarts."""
    wd = resolve(cwd, must_exist=True)
    jid = uuid.uuid4().hex[:8]
    log = JOBS / f"{jid}.log"
    # the wrapper records the exit code; the job's own process group can be killed as a whole
    wrapped = f'( {command}\n) ; echo $? > {JOBS}/{jid}.rc'
    with open(log, "wb") as fh:
        proc = subprocess.Popen(["nice", "-n", "10", "bash", "-c", wrapped], cwd=wd, env=env(), stdin=subprocess.DEVNULL,
                                stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
    for pid, pr in list(PROCS.items()):  # reap finished jobs
        if pr.poll() is not None:
            del PROCS[pid]
    PROCS[proc.pid] = proc
    meta = {"id": jid, "name": name or command[:60], "command": command, "cwd": rel(wd), "pid": proc.pid, "started": time.time()}
    (JOBS / f"{jid}.json").write_text(json.dumps(meta))
    return f"started job {jid} (pid {proc.pid}) in {rel(wd)}; log: job_status(job_id=\"{jid}\")"


@mcp.tool()
def job_status(job_id: str = "", tail: int = 60) -> str:
    """Without job_id: list jobs (newest first) with state. With job_id: state, runtime and the last `tail` log lines."""
    if not job_id:
        jobs = sorted((_job(f.stem) for f in JOBS.glob("*.json")), key=lambda j: -j["started"])[:30]
        if not jobs:
            return "No jobs."
        return "\n".join(f"{j['id']}  {j['state']:<8} {('exit ' + j['exit']) if 'exit' in j else '':<7} "
                         f"{time.strftime('%m-%d %H:%M', time.localtime(j['started']))}  {j['name']}" for j in jobs)
    j = _job(job_id)
    end = j.get("ended", time.time())
    lines = (JOBS / f"{job_id}.log").read_text(errors="replace").splitlines()
    n = max(1, min(int(tail or 60), 400))
    head = (f"job {job_id} · {j['state']}{' · exit ' + j['exit'] if 'exit' in j else ''} · {end - j['started']:.0f}s"
            f" · cwd {j['cwd']}\n$ {j['command']}\n--- last {min(n, len(lines))} of {len(lines)} log lines ---\n")
    return head + clip("\n".join(lines[-n:]))


@mcp.tool()
def job_stop(job_id: str) -> str:
    """Stop a background job (its whole process group)."""
    j = _job(job_id)
    if j["state"] != "running":
        return f"job {job_id} is not running ({j['state']})"
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(j["pid"], sig)
        except (ProcessLookupError, PermissionError):  # gone, or only a zombie left
            break
        if sig == signal.SIGTERM:
            time.sleep(2)
    if (proc := PROCS.pop(j["pid"], None)) is not None:
        proc.wait(timeout=5)
    return f"job {job_id} stopped"


# ---------- files ----------

@mcp.tool()
def list_files(path: str = ".", depth: int = 2) -> str:
    """List a workspace directory as a tree with file sizes (depth 1-5). Start here to see what exists."""
    root = resolve(path, must_exist=True)
    if root.is_file():
        return f"{rel(root)}  {human(root.stat().st_size)}"
    depth = max(1, min(int(depth or 2), 5))
    out, count = [f"{rel(root)}/"], 0

    def walk(d: Path, level: int):
        nonlocal count
        try:
            items = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            out.append("  " * level + "[permission denied]")
            return
        for p in items:
            if count >= 400:
                return
            count += 1
            if p.is_dir() and not p.is_symlink():
                if p.name in SKIP:
                    out.append("  " * level + f"{p.name}/ [skipped]")
                    continue
                out.append("  " * level + f"{p.name}/")
                if level < depth:
                    walk(p, level + 1)
            else:
                try:
                    st = p.stat()
                    out.append("  " * level + f"{p.name}  {human(st.st_size)}  {time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}")
                except OSError:
                    out.append("  " * level + f"{p.name}  [broken link]")

    walk(root, 1)
    if count >= 400:
        out.append("[... listing cut at 400 entries; list a subdirectory]")
    usage = shutil.disk_usage(WS)
    out.append(f"\n[disk: {human(usage.free)} free of {human(usage.total)}]")
    return "\n".join(out) if count else f"{rel(root)}/ is empty"


@mcp.tool()
def read_file(path: str, offset: int = 0) -> str:
    """Read a workspace file as text (paged: pass offset to continue). PDF, DOCX and HTML are converted to text.
    For big tables or binary data use run (head, pandas, ncdump, h5ls, ...)."""
    p = resolve(path, must_exist=True)
    if p.is_dir():
        return list_files(path)
    size = p.stat().st_size
    if size > TEXT_LIMIT:
        return f"{rel(p)} is {human(size)}: too big to read whole; inspect it with run (head, tail, grep, python)."
    raw = p.read_bytes()
    ext = p.suffix.lower()
    if ext == ".pdf" or raw[:5] == b"%PDF-":
        text = pdf_to_text(raw)
    elif ext == ".docx":
        import zipfile
        with zipfile.ZipFile(p) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        text = re.sub(r"<[^>]+>", "", re.sub(r"</w:p>", "\n", xml))
    elif ext in (".html", ".htm"):
        text = html_to_text(raw.decode("utf-8", "replace"))
    elif b"\x00" in raw[:8192]:
        kind = subprocess.run(["file", "-b", str(p)], capture_output=True, text=True).stdout.strip() if shutil.which("file") else "binary"
        return f"{rel(p)}: binary ({kind}), {human(size)}. Inspect it with run (python, ncdump, h5ls, unzip -l, ...)."
    else:
        text = raw.decode("utf-8", "replace")
    return f"{rel(p)} ({human(size)})\n\n" + page(text, int(offset or 0))


@mcp.tool()
def write_file(path: str, content: str, append: bool = False) -> str:
    """Create or overwrite a workspace file (parent directories are created); append=True adds to the end.
    Use for scripts, notes, configs, small data. Organise work as <project>/{data,scripts,results,notes.md}."""
    p = resolve(path)
    if p.is_dir():
        raise IsADirectoryError(f"{rel(p)} is a directory")
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a" if append else "w", encoding="utf-8") as f:
        f.write(content)
    return f"{'appended to' if append else 'wrote'} {rel(p)} ({human(p.stat().st_size)}) · link: /ws/{rel(p)}"


@mcp.tool()
def edit_file(path: str, old: str, new: str, count: int = 1) -> str:
    """Replace exact text in a workspace file. `old` must occur exactly `count` times (default once);
    include enough surrounding lines to make it unique."""
    p = resolve(path, must_exist=True)
    text = p.read_text(encoding="utf-8")
    n = text.count(old)
    if not old or n != int(count or 1):
        return f"ERROR: `old` occurs {n} times in {rel(p)}, expected {count or 1}; nothing changed. Re-read the file and quote it exactly."
    p.write_text(text.replace(old, new), encoding="utf-8")
    return f"edited {rel(p)} ({n} replacement{'s' if n > 1 else ''})"


# ---------- downloads ----------

@mcp.tool()
def download(url: str, path: str = "", max_mb: int = 4096) -> str:
    """Download a URL (http/https) into the workspace without reading it into the chat. Returns size and sha256.
    path: target file or directory (default downloads/<name from URL>). max_mb caps the size (up to 20480).
    For many files or resumable huge downloads use run/job_start with wget -c, curl, aria2c or a script."""
    import httpx
    if not url.startswith(("http://", "https://")):
        return "Only http(s) URLs are supported."
    limit = max(1, min(int(max_mb or 4096), DOWNLOAD_MAX_MB)) * 2**20
    target = resolve(path or "downloads")
    t0, h, n = time.time(), hashlib.sha256(), 0
    with httpx.Client(timeout=httpx.Timeout(60, connect=20), follow_redirects=True, headers={"User-Agent": UA}) as c:
        with c.stream("GET", url) as r:
            if r.status_code >= 400:
                return f"HTTP {r.status_code} for {url}"
            if target.is_dir() or not path or path.endswith("/"):
                cd = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', r.headers.get("content-disposition", ""))
                name = unquote(cd.group(1)) if cd else Path(unquote(urlparse(str(r.url)).path)).name
                target = resolve(f"{rel(target)}/{Path(name).name or 'download'}")
            if int(r.headers.get("content-length") or 0) > limit:
                return f"{url} is {human(int(r.headers['content-length']))}, over max_mb={limit >> 20}; raise max_mb or use job_start + wget -c."
            target.parent.mkdir(parents=True, exist_ok=True)
            part = target.with_name(target.name + ".part")
            with open(part, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    n += len(chunk)
                    if n > limit:
                        f.close()
                        part.unlink()
                        return f"aborted: {url} exceeds max_mb={limit >> 20}"
                    h.update(chunk)
                    f.write(chunk)
            part.replace(target)
            ctype = r.headers.get("content-type", "?")
    dt = time.time() - t0
    return (f"saved {rel(target)} · {human(n)} · {ctype} · {dt:.1f}s ({human(n / max(dt, .001))}/s)\n"
            f"sha256 {h.hexdigest()}\nlink: /ws/{rel(target)}")


if __name__ == "__main__":
    _setup()
    mcp.run()
