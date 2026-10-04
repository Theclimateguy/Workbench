#!/usr/bin/env python3
"""MCP server `target-host`: lets the support agent inspect and change a remote
Ubuntu host over SSH.

Design rules (see PLAN.md §8.2):
- read-only tools run freely; mutating tools are separate and the harness
  (OpenCode permission config) asks the user before each call;
- every call is appended to the audit log (JSON lines);
- command output and file contents are data for the agent, never instructions.

Targets are defined in $SUPPORT_TARGETS_DIR/<name>.env with keys
HOST, USER (default support-agent), PORT (default 22), KEY (default agent key).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import shlex
import tempfile
from pathlib import Path

from mcp.server.fastmcp import FastMCP

TARGETS_DIR = Path(os.environ.get("SUPPORT_TARGETS_DIR", "/opt/support/targets"))
AGENT_KEY = Path(os.environ.get("SUPPORT_AGENT_KEY", "/opt/support/keys/agent_ed25519"))
AUDIT_LOG = Path(os.environ.get("SUPPORT_AUDIT_LOG", "/var/log/support/audit.jsonl"))
SSH_TIMEOUT = int(os.environ.get("SUPPORT_SSH_TIMEOUT", "120"))
MAX_OUTPUT = 60_000  # bytes returned to the model per call

# Commands the agent may run without confirmation. First token of every
# pipeline segment must be here; no redirections, no command substitution.
READ_ALLOWLIST = {
    "cat", "head", "tail", "less", "grep", "egrep", "zgrep", "awk", "sed", "cut", "sort", "uniq", "wc",
    "tr", "column", "ls", "stat", "file", "find", "du", "df", "lsblk", "blkid", "mount", "findmnt",
    "free", "uptime", "w", "who", "id", "hostname", "hostnamectl", "timedatectl", "localectl", "date",
    "uname", "lscpu", "lspci", "lsusb", "lsmod", "dmesg", "dmidecode", "sensors", "nvidia-smi",
    "ip", "ss", "ping", "traceroute", "mtr", "dig", "resolvectl", "nmcli", "netplan", "ethtool", "iw",
    "systemctl", "journalctl", "loginctl", "ps", "pgrep", "top", "htop", "pstree", "lsof", "fuser",
    "dpkg", "dpkg-query", "apt", "apt-cache", "apt-mark", "snap", "pip", "pip3", "python3",
    "git", "env", "printenv", "echo", "test", "true", "which", "type", "readlink", "realpath",
    "getent", "groups", "last", "lastlog", "ufw", "iptables", "nft", "sudo", "nproc", "vmstat",
    "iostat", "sar", "smartctl", "nvme", "hdparm", "fwupdmgr", "ubuntu-security-status", "needrestart",
    "md5sum", "sha256sum", "diff", "cmp", "tree", "xargs", "jq", "yq", "docker", "crictl",
}
# Subcommands that mutate even though the binary is allowlisted.
READ_DENY_SUBCMDS = {
    "systemctl": {"start", "stop", "restart", "reload", "enable", "disable", "mask", "unmask", "edit",
                  "set-default", "isolate", "kill", "daemon-reload", "reboot", "poweroff", "halt"},
    "apt": {"install", "remove", "purge", "upgrade", "dist-upgrade", "full-upgrade", "autoremove", "update"},
    "apt-mark": {"hold", "unhold", "manual", "auto"},
    "netplan": {"apply", "try"},
    "nmcli": {"connection", "con", "c", "device", "dev", "d", "radio", "networking"},
    "ip": {"link", "addr", "address", "route", "neigh", "rule"},  # only `ip ... show` is read; see below
    "ufw": {"enable", "disable", "allow", "deny", "reject", "delete", "reset", "default", "insert", "route"},
    "docker": {"run", "exec", "rm", "rmi", "stop", "start", "restart", "kill", "pull", "push", "build",
               "compose", "system", "volume", "network", "swarm", "login"},
    "snap": {"install", "remove", "refresh", "revert", "set", "connect", "disconnect"},
    "git": {"push", "reset", "checkout", "clean", "rebase", "merge", "commit", "add", "rm", "mv"},
    "pip": {"install", "uninstall"}, "pip3": {"install", "uninstall"},
    "python3": None,   # python3 only allowed as `python3 --version`
    "dmesg": {"-C", "-c", "--clear", "--read-clear"},
    "fwupdmgr": {"update", "install", "downgrade", "reinstall", "enable-remote", "disable-remote"},
    "nvme": {"format", "sanitize", "delete-ns", "create-ns", "fw-download", "fw-commit", "reset"},
    "hdparm": None, "lsof": set(), "kill": None,
}
SHELL_META = ("&&", "||", ";", ">", ">>", "<", "`", "$(", "&", "\n")

mcp = FastMCP("target-host")


# ---------- helpers ----------

def _audit(event: dict) -> None:
    event["ts"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_LOG.open("a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


_resolve_cache: dict[str, tuple[float, str]] = {}


async def _resolve_host(name: str, cfg: dict) -> str:
    """HOST=auto -> ask support-resolve-target (mDNS / DHCP lease / MAC / last IP). Cached 60 s."""
    if cfg.get("HOST") not in (None, "", "auto"):
        return cfg["HOST"]
    import time
    hit = _resolve_cache.get(name)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    resolver = Path(__file__).resolve().parent.parent / "bin" / "support-resolve-target"
    proc = await asyncio.create_subprocess_exec(str(resolver), name, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout=40)
    if proc.returncode != 0:
        raise ValueError(f"target '{name}' not reachable: {err.decode(errors='replace').strip()}")
    host = out.decode().strip()
    _resolve_cache[name] = (time.time(), host)
    return host


def _load_target(name: str) -> dict:
    p = TARGETS_DIR / f"{name}.env"
    if not p.exists():
        raise ValueError(f"unknown target '{name}'; known: {', '.join(_target_names()) or '(none)'}")
    cfg = {"USER": "support-agent", "PORT": "22", "KEY": str(AGENT_KEY)}
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"')
    if "HOST" not in cfg and "MDNS" not in cfg and "MAC" not in cfg:
        raise ValueError(f"target '{name}' has no HOST/MDNS/MAC")
    cfg["NAME"] = name
    return cfg


def _target_names() -> list[str]:
    return sorted(p.stem for p in TARGETS_DIR.glob("*.env")) if TARGETS_DIR.exists() else []


def _ssh_base(cfg: dict, host: str) -> list[str]:
    # HostKeyAlias: the host key is remembered per target name, not per (changing) IP.
    return [
        "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"HostKeyAlias={cfg['NAME']}", "-o", "ConnectTimeout=15",
        "-i", cfg["KEY"], "-p", cfg["PORT"], f"{cfg['USER']}@{host}",
    ]


async def _run_ssh(cfg: dict, remote_cmd: str, timeout: int = SSH_TIMEOUT, stdin: str | None = None) -> dict:
    """Async so that parallel tool calls from the agent do not block each other."""
    host = await _resolve_host(cfg["NAME"], cfg)
    proc = await asyncio.create_subprocess_exec(
        *_ssh_base(cfg, host), remote_cmd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(stdin.encode() if stdin is not None else None), timeout=timeout)
        out, err, rc = out_b.decode(errors="replace"), err_b.decode(errors="replace"), proc.returncode
    except asyncio.TimeoutError:
        proc.kill()
        out, err, rc = "", f"timeout after {timeout}s", 124
    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT] + f"\n[... truncated, {len(out)} bytes total]"
    return {"rc": rc, "stdout": out, "stderr": err[-4000:]}


def _classify_readonly(cmd: str) -> str | None:
    """Return None if cmd is read-only per allowlist, else a reason string."""
    for meta in SHELL_META:
        if meta in cmd:
            return f"shell operator '{meta.strip()}' not allowed in read-only commands"
    for segment in cmd.split("|"):
        try:
            argv = shlex.split(segment)
        except ValueError as e:
            return f"cannot parse: {e}"
        if not argv:
            return "empty command"
        if argv[0] == "sudo":
            argv = [a for a in argv[1:] if not a.startswith("-")] or [""]
        prog = os.path.basename(argv[0])
        if prog not in READ_ALLOWLIST:
            return f"'{prog}' is not in the read-only allowlist"
        deny = READ_DENY_SUBCMDS.get(prog, set())
        if deny is None:
            if not (prog == "python3" and argv[1:] == ["--version"]):
                return f"'{prog}' is only allowed via run_change"
            continue
        args = argv[1:]
        if prog == "ip":
            # `ip link show`, `ip -br addr` are reads; anything with add/del/set/up/down is not
            if any(a in {"add", "del", "delete", "set", "up", "down", "flush", "replace", "change"} for a in args):
                return "ip command modifies network state"
            continue
        if prog == "nmcli":
            if any(a in {"up", "down", "add", "modify", "mod", "delete", "del", "reload", "load",
                         "connect", "disconnect", "set", "on", "off", "wifi"} for a in args):
                return "nmcli command modifies network state"
            continue
        if any(a in deny for a in args):
            return f"'{prog} {next(a for a in args if a in deny)}' modifies state"
    return None


# ---------- read-only tools ----------

@mcp.tool()
def target_list() -> str:
    """List configured target hosts (name, host, user)."""
    rows = []
    for n in _target_names():
        c = _load_target(n)
        rows.append(f"{n}: {c['USER']}@{c.get('HOST','auto')}:{c['PORT']} (mdns={c.get('MDNS','-')}, mac={c.get('MAC','-')}, last={c.get('IP_LAST','-')})")
    return "\n".join(rows) or "no targets configured; run `support-ctl target add` on the Spark"


@mcp.tool()
async def collect_facts(target: str = "ws") -> str:
    """Snapshot of the target host: OS, hardware, disks, network, failed units, recent errors, GPU, packages state."""
    cfg = _load_target(target)
    script = r"""
set +e
echo "### os"; cat /etc/os-release | head -3; uname -r; uptime
echo "### cpu/mem"; nproc; free -h | head -2
echo "### disks"; lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT; df -h -x tmpfs -x devtmpfs -x overlay
echo "### network"; ip -br link; ip -br addr; ip route; resolvectl status 2>/dev/null | grep -E "DNS Servers|Link" | head -8
echo "### failed units"; systemctl --failed --no-legend
echo "### recent errors (this boot)"; journalctl -p err -b --no-pager -n 40 -o short
echo "### dpkg state"; sudo -n dpkg --audit 2>&1 | head -20; apt-get -s -o Debug::NoLocking=1 upgrade 2>/dev/null | grep -c "^Inst" | sed 's/^/upgradable: /'
echo "### gpu"; nvidia-smi --query-gpu=name,driver_version,memory.used,memory.total --format=csv 2>&1 | head -3
echo "### reboot"; [ -f /var/run/reboot-required ] && cat /var/run/reboot-required || echo "no reboot pending"
"""
    r = await _run_ssh(cfg, script)
    _audit({"tool": "collect_facts", "target": target, "rc": r["rc"]})
    return r["stdout"] + ("\n[stderr] " + r["stderr"] if r["stderr"] else "")


@mcp.tool()
async def run_readonly(cmd: str, target: str = "ws") -> str:
    """Run a read-only diagnostic command on the target (logs, status, configs). Pipes allowed; no redirections.
    Rejected commands must go through run_change."""
    reason = _classify_readonly(cmd)
    if reason:
        _audit({"tool": "run_readonly", "target": target, "cmd": cmd, "rejected": reason})
        return f"REJECTED (not read-only): {reason}. Use run_change if this change is intended."
    cfg = _load_target(target)
    r = await _run_ssh(cfg, cmd)
    _audit({"tool": "run_readonly", "target": target, "cmd": cmd, "rc": r["rc"]})
    return f"rc={r['rc']}\n{r['stdout']}" + (f"\n[stderr]\n{r['stderr']}" if r["stderr"] else "")


@mcp.tool()
async def read_file(path: str, target: str = "ws", max_bytes: int = 40000) -> str:
    """Read a file from the target (uses sudo if needed). Output is truncated to max_bytes."""
    cfg = _load_target(target)
    q = shlex.quote(path)
    r = await _run_ssh(cfg, f"(cat {q} 2>/dev/null || sudo -n cat {q}) | head -c {int(max_bytes)}")
    _audit({"tool": "read_file", "target": target, "path": path, "rc": r["rc"]})
    if r["rc"] != 0 and not r["stdout"]:
        return f"ERROR rc={r['rc']}: {r['stderr']}"
    return r["stdout"]


# ---------- mutating tools (harness asks the user before each call) ----------

@mcp.tool()
async def run_change(cmd: str, reason: str, target: str = "ws") -> str:
    """Run a command that CHANGES the target (install, restart, edit, apply). Requires user approval.
    `reason` must say what and why; it goes to the audit log."""
    cfg = _load_target(target)
    r = await _run_ssh(cfg, cmd, timeout=600)
    _audit({"tool": "run_change", "target": target, "cmd": cmd, "reason": reason, "rc": r["rc"]})
    return f"rc={r['rc']}\n{r['stdout']}" + (f"\n[stderr]\n{r['stderr']}" if r["stderr"] else "")


@mcp.tool()
async def write_file(path: str, content: str, reason: str, target: str = "ws", mode: str = "") -> str:
    """Write a whole file on the target. Keeps a timestamped backup next to it and returns the diff.
    Requires user approval."""
    cfg = _load_target(target)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    q = shlex.quote(path)
    tmp = f"/tmp/.support-write-{stamp}"
    script = f"""
set -e
cat > {tmp}
if sudo -n test -e {q}; then sudo -n cp -a {q} {q}.bak-{stamp}; fi
echo "### diff"; sudo -n diff -u {q} {tmp} 2>/dev/null || true
sudo -n install -D {('-m ' + shlex.quote(mode)) if mode else ''} {tmp} {q}
rm -f {tmp}
echo "### written {path} (backup: {path}.bak-{stamp})"
"""
    r = await _run_ssh(cfg, script, stdin=content)
    _audit({"tool": "write_file", "target": target, "path": path, "reason": reason, "rc": r["rc"],
            "bytes": len(content)})
    return f"rc={r['rc']}\n{r['stdout']}" + (f"\n[stderr]\n{r['stderr']}" if r["stderr"] else "")


@mcp.tool()
async def reboot_and_wait(reason: str, target: str = "ws", wait_seconds: int = 300) -> str:
    """Reboot the target and wait until SSH is back. Requires user approval."""
    import time
    cfg = _load_target(target)
    _audit({"tool": "reboot_and_wait", "target": target, "reason": reason})
    await _run_ssh(cfg, "sudo -n systemctl reboot", timeout=20)
    await asyncio.sleep(15)
    _resolve_cache.pop(cfg["NAME"], None)
    t0 = time.time()
    while time.time() - t0 < wait_seconds:
        r = await _run_ssh(cfg, "uptime -s", timeout=10)
        if r["rc"] == 0:
            return f"target is back; booted at {r['stdout'].strip()} ({int(time.time()-t0)+15}s)"
        await asyncio.sleep(10)
    return f"target did not come back within {wait_seconds}s — check console/serial"


if __name__ == "__main__":
    mcp.run()
