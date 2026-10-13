#!/usr/bin/env python3
"""Smoke test for the device connectors and action classes of the target-host MCP server.

Starts simulated devices on this machine (no hardware needed):
  http    a tiny HTTP server (GET /status, POST /set)
  modbus  a pymodbus TCP server (unit 1, holding registers 0..99)
  serial  a socat pty pair with a responder on the far end          (Linux with socat; skipped otherwise)
  can     vcan0 with a responder that answers id 0x123 on 0x124      (Linux; created with sudo if missing)
then drives the MCP server over stdio exactly like OpenCode does and checks:
  observe works per type; device_act is refused for physical buses; device_actuate needs a rollback plan and
  reads back; recover works; run_change refuses power/firmware commands; the audit log records the class;
  workbench_run executes a script in the sandbox with network=none and can reach only the device with network=device.

  /opt/support/venv/bin/python tests/connectors_smoke.py [--ssh-target self] [--no-workbench]
"""
from __future__ import annotations

import argparse
import asyncio
import http.server
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
SERVER = os.environ.get("TARGET_HOST_MCP", "/opt/support/mcp/target_host.py" if Path("/opt/support/mcp/target_host.py").exists()
                        else str(HERE.parent / "agent" / "mcp" / "target_host.py"))
PYTHON = os.environ.get("TARGET_HOST_PY", sys.executable)
LINUX = platform.system() == "Linux"


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1)); return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0)); return s.getsockname()[1]


# ---------------------------------------------------------------- simulated devices

class _H(http.server.BaseHTTPRequestHandler):
    state = {"relay": 0}

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        self._send(200, {"path": self.path, "status": "ok", **self.state})

    def do_HEAD(self):
        self.send_response(200); self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        self.state.update(body); self._send(200, {"set": body, **self.state})


def start_http(port: int):
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def start_modbus(port: int):
    from pymodbus.datastore import ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext
    from pymodbus.server import StartTcpServer
    import warnings
    warnings.filterwarnings("ignore")
    # pymodbus 3.16: datablock addresses are 1-based (register 0 on the wire = index 1)
    dev = ModbusDeviceContext(hr=ModbusSequentialDataBlock(1, [7, 11, 13, 17] + [0] * 96),
                              ir=ModbusSequentialDataBlock(1, [250, 251] + [0] * 98),
                              co=ModbusSequentialDataBlock(1, [0] * 16), di=ModbusSequentialDataBlock(1, [1, 0] * 8))
    ctx = ModbusServerContext(devices={1: dev}, single=False)
    threading.Thread(target=lambda: StartTcpServer(context=ctx, address=("127.0.0.1", port)), daemon=True).start()
    time.sleep(0.8)


def start_serial(tmp: Path):
    """socat pty pair: /tmp/.../devA is the 'device' the connector opens; a responder lives on devB."""
    if not LINUX or not shutil.which("socat"):
        return None
    a, b = tmp / "devA", tmp / "devB"
    proc = subprocess.Popen(["socat", "-d", "-d", f"pty,raw,echo=0,link={a}", f"pty,raw,echo=0,link={b}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if a.exists() and b.exists():
            break
        time.sleep(0.1)
    import serial
    far = serial.Serial(str(b), 115200, timeout=0.1)

    def responder():
        last = time.time()
        while proc.poll() is None:
            line = far.readline()
            if line:
                far.write(b"echo: " + line.strip() + b"\n")
            if time.time() - last > 0.7:
                far.write(b"tick\n"); last = time.time()
    threading.Thread(target=responder, daemon=True).start()
    return proc, a


def start_can():
    if not LINUX:
        return None
    if subprocess.run(["ip", "link", "show", "vcan0"], capture_output=True).returncode != 0:
        r = subprocess.run("sudo -n modprobe vcan && sudo -n ip link add vcan0 type vcan && sudo -n ip link set vcan0 up",
                           shell=True, capture_output=True, text=True)
        if r.returncode != 0:
            print("  (no vcan0 and cannot create it:", (r.stderr or r.stdout).strip()[:80], ") — CAN skipped")
            return None
    import can
    bus = can.Bus(channel="vcan0", interface="socketcan")

    def responder():
        last = time.time()
        while True:
            m = bus.recv(timeout=0.2)
            if m is not None and m.arbitration_id == 0x123:
                bus.send(can.Message(arbitration_id=0x124, data=bytes([0xAA]) + bytes(m.data[:7]), is_extended_id=False))
            if time.time() - last > 0.5:
                bus.send(can.Message(arbitration_id=0x7E8, data=b"\x01\x02\x03", is_extended_id=False)); last = time.time()
    threading.Thread(target=responder, daemon=True).start()
    return bus


# ---------------------------------------------------------------- the test

async def main(a) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="wb-smoke-"))
    targets = tmp / "targets"; targets.mkdir()
    audit = tmp / "audit.jsonl"
    ip = lan_ip()
    hp, mp = free_port(), free_port()
    start_http(hp); start_modbus(mp)
    (targets / "web.env").write_text(f"TYPE=http\nURL=http://127.0.0.1:{hp}\n")
    (targets / "weblan.env").write_text(f"TYPE=http\nURL=http://{ip}:{hp}\n")      # reachable from the sandbox container
    (targets / "plc.env").write_text(f"TYPE=modbus\nHOST=127.0.0.1\nPORT={mp}\nUNIT=1\n")
    ser = start_serial(tmp)
    if ser:
        (targets / "uart.env").write_text(f"TYPE=serial\nPORT={ser[1]}\nBAUD=115200\n")
    canbus = start_can()
    if canbus:
        (targets / "bus.env").write_text("TYPE=can\nIFACE=vcan0\nBITRATE=500000\n")
    if a.ssh_target:
        src = Path(os.environ.get("SUPPORT_TARGETS_DIR", "/opt/support/targets")) / f"{a.ssh_target}.env"
        shutil.copy(src, targets / f"{a.ssh_target}.env")
    env = {**os.environ, "SUPPORT_TARGETS_DIR": str(targets), "SUPPORT_AUDIT_LOG": str(audit),
           "SUPPORT_WORKBENCH_DIR": str(tmp / "workbench")}
    fails = 0

    def check(cond, label, detail=""):
        nonlocal fails
        print(("ok   " if cond else "FAIL ") + label + (f"  -> {detail}" if detail and not cond else ""))
        fails += 0 if cond else 1

    async with stdio_client(StdioServerParameters(command=PYTHON, args=[SERVER], env=env)) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = sorted(t.name for t in (await s.list_tools()).tools)
            need = {"device_list", "observe", "device_act", "device_actuate", "device_recover", "scan", "workbench_run",
                    "target_list", "collect_facts", "run_readonly", "read_file", "run_change", "write_file", "reboot_and_wait"}
            check(need <= set(tools), "tool set", str(need - set(tools)))

            async def call(name, **kw):
                res = await s.call_tool(name, kw)
                return "".join(c.text for c in res.content if hasattr(c, "text"))

            out = await call("device_list")
            check("web: http" in out and "plc: modbus-tcp" in out, "device_list shows types", out)

            # ---- http
            out = await call("observe", device="web", query="GET /status")
            check("-> 200" in out and '"status": "ok"' in out, "http observe GET", out)
            out = await call("device_act", device="web", action='POST /set {"relay": 1}', reason="test")
            check("-> 200" in out and '"relay": 1' in out, "http device_act POST (config class)", out)

            # ---- modbus
            out = await call("observe", device="plc", query="holding 0 4")
            check("    0       7" in out and "    3      17" in out, "modbus observe holding", out)
            out = await call("observe", device="plc", query="input 0 2")
            check("250" in out and "251" in out, "modbus observe input", out)
            out = await call("device_act", device="plc", action="write_register 5 42", reason="test")
            check(out.startswith("REJECTED") and "ACTUATION" in out, "modbus device_act refused (actuation class)", out)
            out = await call("device_actuate", device="plc", action="write_register 5 42", reason="test", rollback="")
            check(out.startswith("REJECTED") and "rollback" in out, "device_actuate without rollback refused", out)
            out = await call("device_actuate", device="plc", action="write_register 5 42", reason="test", rollback="write_register 5 0 (previous value 0)")
            check("read-back 42" in out, "modbus device_actuate write + read-back", out)
            out = await call("device_actuate", device="plc", action="write_coil 2 1", reason="test", rollback="write_coil 2 0 restores the previous state")
            check("read-back 1" in out, "modbus write_coil", out)
            out = await call("observe", device="plc", query="coils 0 4")
            check(out.strip().endswith("0 0 1 0"), "modbus coils read", out)
            out = await call("device_recover", device="plc", reason="test")
            check("re-established" in out, "modbus device_recover", out)

            # ---- serial
            if ser:
                out = await call("observe", device="uart", seconds=2)
                check("tick" in out, "serial observe (passive listen)", out)
                out = await call("observe", device="uart", query="ask:help")
                check(out.startswith("ERROR") and "passive" in out, "serial observe refuses to send", out)
                out = await call("device_act", device="uart", action="send:hello", reason="t")
                check(out.startswith("REJECTED"), "serial device_act refused (actuation class)", out)
                out = await call("device_actuate", device="uart", action="send:hello", reason="t", rollback="console command, no state change", seconds=1.5)
                check("echo: hello" in out, "serial device_actuate send + reply", out)
                out = await call("device_recover", device="uart", reason="t")
                check("reset pulse" in out, "serial device_recover (DTR pulse)", out)
            else:
                print("skip serial (needs Linux + socat)")

            # ---- can
            if canbus:
                out = await call("observe", device="bus", query="dump", seconds=2)
                check("7E8:" in out and "7E8#010203" in out, "can observe dump", out)
                out = await call("observe", device="bus", query="dump id=0x7E8", seconds=1)
                check("7E8#010203" in out and "124#" not in out, "can observe filter", out)
                out = await call("device_actuate", device="bus", action="send 123#DEADBEEF", reason="t", rollback="no persistent effect on the simulated node", seconds=1)
                check("124#AADEADBEEF" in out, "can device_actuate send + reply", out)
            else:
                print("skip can (needs Linux vcan)")

            # ---- ssh classes
            if a.ssh_target:
                out = await call("run_change", cmd="sudo reboot", reason="t", target=a.ssh_target)
                check(out.startswith("REJECTED") and "ACTUATION" in out, "run_change refuses power commands", out)
                out = await call("run_readonly", cmd="uptime", target=a.ssh_target)
                check(out.startswith("rc=0"), "run_readonly on ssh target", out)
                out = await call("observe", device=a.ssh_target, query="hostname")
                check(out.startswith("rc=0"), "observe routes to run_readonly for ssh", out)
            else:
                print("skip ssh (pass --ssh-target self on the box)")

            # ---- scan
            if shutil.which("support-ctl") or Path("/usr/local/bin/support-ctl").exists():
                out = await call("scan", what="usb")
                check("== USB" in out, "scan usb", out)
            else:
                print("skip scan (support-ctl not installed here)")

            # ---- workbench
            if not a.no_workbench and shutil.which("docker") and subprocess.run(["docker", "image", "inspect", os.environ.get("SUPPORT_WORKBENCH_IMAGE", "workbench:local")], capture_output=True).returncode == 0:
                out = await call("workbench_run", script="import serial, can, pymodbus, scapy\nprint('libs ok')\n", reason="t", timeout=60)
                check("rc=0" in out and "libs ok" in out, "workbench_run python network=none", out)
                out = await call("workbench_run", script=f"curl -s -m 3 http://{ip}:{hp}/x && echo REACHED || echo BLOCKED", reason="t", language="bash", timeout=30)
                check("BLOCKED" in out, "workbench network=none blocks the device", out)
                out = await call("workbench_run", script=f"curl -s -m 5 http://{ip}:{hp}/x && echo REACHED || echo BLOCKED; curl -s -m 3 http://1.1.1.1/ -o /dev/null && echo LEAK || echo OUTSIDE-BLOCKED",
                                 reason="t", language="bash", devices="weblan", network="device", timeout=40)
                check("REACHED" in out and "OUTSIDE-BLOCKED" in out, "workbench network=device reaches only the device", out)
                out = await call("workbench_run", script="import json;print(json.load(open('/work/devices.json'))['plc']['PORT'])", reason="t", devices="plc", timeout=30)
                check(str(mp) in out, "workbench devices.json", out)
            else:
                print("skip workbench (docker or workbench:local image missing)")

    rows = [json.loads(ln) for ln in audit.read_text().splitlines()] if audit.exists() else []
    check(any(r.get("tool") == "device_actuate" and r.get("class") == "actuation" and r.get("rollback") for r in rows), "audit records class + rollback")
    print("FAILURES:", fails)
    shutil.rmtree(tmp, ignore_errors=True)
    return fails


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ssh-target", default="")
    ap.add_argument("--no-workbench", action="store_true")
    raise SystemExit(asyncio.run(main(ap.parse_args())))
