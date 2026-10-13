"""Device connectors for the target-host MCP server (PLAN.md §4, phase A).

A *device* is described by /opt/support/targets/<name>.env. `TYPE=` selects the connector:

  ssh     HOST|MDNS|MAC, USER, PORT, KEY          Linux host over SSH (the original target)
  serial  PORT=/dev/ttyUSB0 BAUD=115200 EOL=\\n    UART / USB-serial console
  can     IFACE=can0 BITRATE=500000               SocketCAN bus
  modbus  HOST=10.77.0.5 PORT=502 UNIT=1          Modbus TCP, or PORT=/dev/ttyUSB1 BAUD=9600 for RTU
  http    URL=http://10.77.0.9 AUTH=user:pw        device web API (GET = observe; POST/PUT = act)

Every connector implements the same contract:

  observe(query)             read-only; runs without confirmation
  act(action)                CONFIG class: changes settings / files / services, approval per call
  actuate(action)            ACTUATION class: moves, powers, flashes or talks to a physical bus;
                             approval per call, never "always", a rollback plan is mandatory
  recover()                  ACTUATION class: reboot / reset pulse / bus restart

Which class a connector's writes belong to is fixed here, not chosen by the model:
serial, can and modbus writes are always ACTUATION (they reach hardware directly);
ssh and http writes are CONFIG, except commands that power-cycle or flash (see POWER_FIRMWARE_RE),
which the ssh connector refuses in act() and accepts only in actuate().

All connector work is synchronous and is run in a thread by the MCP server.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

TYPES = ("ssh", "serial", "can", "modbus", "http")
CONFIG, ACTUATION = "config", "actuation"

# ssh commands that are actuation (power / firmware / disks) even though they look like plain commands.
POWER_FIRMWARE_RE = re.compile(
    r"(^|[\s;&|(])(reboot|shutdown|poweroff|halt|init\s+[06]|systemctl\s+(reboot|poweroff|halt|kexec|suspend|hibernate)"
    r"|esptool(\.py)?|openocd|probe-rs|st-flash|dfu-util|avrdude|fwupdmgr\s+(install|update|downgrade)|flashrom"
    r"|nvidia-smi\s+.*(-r|--gpu-reset)|dd\s+.*of=/dev/|mkfs|wipefs|sgdisk|parted|fdisk|nvme\s+(format|sanitize)"
    r"|ipmitool\s+.*(power|chassis)|echo\s+\S+\s*>\s*/sys/class/gpio)(\s|$)")


class ConnectorError(Exception):
    pass


def _f(cfg: dict, key: str, default=None):
    v = cfg.get(key, default)
    return v if v not in ("", None) else default


# ---------------------------------------------------------------- serial

class Serial:
    cls_act = ACTUATION

    def __init__(self, cfg: dict):
        self.port = _f(cfg, "PORT", "/dev/ttyUSB0")
        self.baud = int(_f(cfg, "BAUD", 115200))
        self.eol = _f(cfg, "EOL", "\\n").encode().decode("unicode_escape")

    def _open(self, timeout=0.2):
        import serial  # pyserial, lazy so the server starts without it
        try:
            return serial.Serial(self.port, self.baud, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            raise ConnectorError(f"cannot open {self.port} @ {self.baud}: {e}") from e

    def _read_for(self, s, seconds: float) -> str:
        buf, t0 = bytearray(), time.time()
        while time.time() - t0 < seconds:
            chunk = s.read(4096)
            if chunk:
                buf += chunk
            if len(buf) > 60_000:
                break
        return buf.decode(errors="replace")

    def observe(self, query: str, seconds: float) -> str:
        """Passive listen only. Sending anything to a console is actuate('send:...')."""
        if query and query not in ("listen", ""):
            raise ConnectorError("serial observe is passive: query must be empty or 'listen'; to send text use device_actuate 'send:<text>'")
        with self._open() as s:
            out = self._read_for(s, seconds)
        return out or f"(nothing received on {self.port} in {seconds:.0f}s)"

    def actuate(self, action: str, seconds: float) -> str:
        """send:<text>  (EOL appended) | hex:<bytes> | dtr:0|1 | rts:0|1 | break"""
        kind, _, arg = action.partition(":")
        with self._open() as s:
            if kind == "send":
                s.write(arg.encode() + self.eol.encode())
            elif kind == "hex":
                s.write(bytes.fromhex(arg.replace(" ", "")))
            elif kind in ("dtr", "rts"):
                setattr(s, kind, arg.strip() == "1")
            elif kind == "break":
                s.send_break(0.25)
            else:
                raise ConnectorError("serial action must be send:<text> | hex:<bytes> | dtr:0|1 | rts:0|1 | break")
            s.flush()
            reply = self._read_for(s, seconds)
        return reply or "(no reply)"

    def recover(self) -> str:
        """Reset pulse on DTR/RTS (Arduino, ESP32 and most dev boards reset on DTR)."""
        with self._open() as s:
            s.dtr = False; s.rts = False; time.sleep(0.1)
            s.dtr = True; s.rts = True; time.sleep(0.1)
            s.dtr = False; time.sleep(0.3)
            out = self._read_for(s, 3.0)
        return "reset pulse sent (DTR/RTS)\n" + (out or "(nothing received after reset)")

    def docker_args(self) -> list[str]:
        return ["--device", self.port] if os.path.exists(self.port) else []


# ---------------------------------------------------------------- CAN

class Can:
    cls_act = ACTUATION

    def __init__(self, cfg: dict):
        self.iface = _f(cfg, "IFACE", "can0")
        self.bitrate = int(_f(cfg, "BITRATE", 500000))

    def _bus(self):
        import can  # python-can
        try:
            return can.Bus(channel=self.iface, interface="socketcan")
        except Exception as e:  # noqa: BLE001
            raise ConnectorError(f"cannot open {self.iface}: {e} (support-ctl device test, or device_recover to bring the link up)") from e

    def observe(self, query: str, seconds: float) -> str:
        """dump [id=0x123] — frames seen for `seconds`; empty query = dump everything."""
        want = None
        m = re.search(r"id=(0x[0-9a-fA-F]+|\d+)", query or "")
        if m:
            want = int(m.group(1), 0)
        bus = self._bus()
        lines, counts, t0 = [], {}, time.time()
        try:
            while time.time() - t0 < seconds:
                msg = bus.recv(timeout=0.2)
                if msg is None:
                    continue
                if want is not None and msg.arbitration_id != want:
                    continue
                counts[msg.arbitration_id] = counts.get(msg.arbitration_id, 0) + 1
                if len(lines) < 400:
                    lines.append(f"{msg.timestamp - t0:8.3f}  {self.iface}  {msg.arbitration_id:03X}#{msg.data.hex().upper()}"
                                 + ("  EXT" if msg.is_extended_id else "") + ("  ERR" if msg.is_error_frame else ""))
        finally:
            bus.shutdown()
        state = subprocess.run(["ip", "-d", "-br", "link", "show", self.iface], capture_output=True, text=True).stdout.strip()
        summary = ", ".join(f"{i:03X}:{n}" for i, n in sorted(counts.items())[:40]) or "no frames"
        return f"link: {state or '?'}\nframes in {seconds:.0f}s by id: {summary}\n" + "\n".join(lines)

    def actuate(self, action: str, seconds: float) -> str:
        """send <id>#<hex>  (candump/cansend syntax, e.g. send 123#DEADBEEF or send 1F334455#01)"""
        m = re.fullmatch(r"\s*send\s+([0-9a-fA-F]+)#([0-9a-fA-F]*)\s*", action)
        if not m:
            raise ConnectorError("can action must be: send <id-hex>#<data-hex>")
        import can
        arb, data = m.group(1), bytes.fromhex(m.group(2))
        msg = can.Message(arbitration_id=int(arb, 16), data=data, is_extended_id=len(arb) > 3)
        bus = self._bus()
        try:
            bus.send(msg, timeout=1.0)
            reply = []
            t0 = time.time()
            while time.time() - t0 < seconds and len(reply) < 50:
                r = bus.recv(timeout=0.2)
                if r is not None:
                    reply.append(f"{r.arbitration_id:03X}#{r.data.hex().upper()}")
        finally:
            bus.shutdown()
        return f"sent {arb.upper()}#{data.hex().upper()}\nreplies within {seconds:.0f}s: " + (", ".join(reply) or "none")

    def recover(self) -> str:
        """Bus-off recovery: bring the interface down and up again with its bitrate."""
        r = subprocess.run(["sudo", "-n", "/opt/support/bin/support-priv", "can-restart", self.iface, str(self.bitrate)],
                           capture_output=True, text=True, timeout=20)
        return (r.stdout + r.stderr).strip() or f"{self.iface} restarted"

    def docker_args(self) -> list[str]:
        return ["--network", "host"]       # SocketCAN interfaces live in the host network namespace


# ---------------------------------------------------------------- Modbus

class Modbus:
    cls_act = ACTUATION

    def __init__(self, cfg: dict):
        self.host = _f(cfg, "HOST")
        self.port = _f(cfg, "PORT", "502")
        self.unit = int(_f(cfg, "UNIT", 1))
        self.baud = int(_f(cfg, "BAUD", 9600))
        self.rtu = str(self.port).startswith("/dev/")

    def _client(self):
        from pymodbus.client import ModbusSerialClient, ModbusTcpClient
        c = ModbusSerialClient(self.port, baudrate=self.baud, timeout=2) if self.rtu \
            else ModbusTcpClient(self.host, port=int(self.port), timeout=3)
        if not c.connect():
            raise ConnectorError(f"modbus: cannot connect to {self.port if self.rtu else f'{self.host}:{self.port}'}")
        return c

    @staticmethod
    def _check(rr, what):
        if rr.isError():
            raise ConnectorError(f"modbus {what}: {rr}")
        return rr

    def observe(self, query: str, seconds: float) -> str:
        """holding A N | input A N | coils A N | discrete A N | id   (A = start address, N = count; unit from the device file)"""
        q = (query or "holding 0 10").split()
        kind = q[0]
        c = self._client()
        try:
            if kind == "id":
                rr = c.read_device_information(device_id=self.unit)
                if rr.isError():
                    return f"device information not supported: {rr}"
                return "\n".join(f"{k}: {v.decode(errors='replace') if isinstance(v, bytes) else v}" for k, v in rr.information.items())
            addr, n = int(q[1], 0), int(q[2]) if len(q) > 2 else 10
            n = max(1, min(n, 125))
            fn = {"holding": c.read_holding_registers, "input": c.read_input_registers,
                  "coils": c.read_coils, "discrete": c.read_discrete_inputs}.get(kind)
            if not fn:
                raise ConnectorError("modbus query must be: holding|input|coils|discrete <addr> <count> | id")
            rr = self._check(fn(addr, count=n, device_id=self.unit), f"read {kind} {addr}+{n}")
            if kind in ("coils", "discrete"):
                vals = [int(b) for b in rr.bits[:n]]
                return f"{kind} {addr}..{addr+n-1} (unit {self.unit}): " + " ".join(map(str, vals))
            regs = rr.registers
            rows = [f"{addr+i:5d}  {v:6d}  0x{v:04X}" + (f"  (int16 {v-65536})" if v > 32767 else "") for i, v in enumerate(regs)]
            return f"{kind} registers {addr}..{addr+n-1} (unit {self.unit}):\n addr   dec     hex\n" + "\n".join(rows)
        finally:
            c.close()

    def actuate(self, action: str, seconds: float) -> str:
        """write_register A V | write_registers A V1,V2,... | write_coil A 0|1"""
        q = action.split()
        if len(q) < 3:
            raise ConnectorError("modbus action must be: write_register <addr> <value> | write_registers <addr> <v1,v2,..> | write_coil <addr> 0|1")
        kind, addr = q[0], int(q[1], 0)
        c = self._client()
        try:
            if kind == "write_register":
                self._check(c.write_register(addr, int(q[2], 0) & 0xFFFF, device_id=self.unit), action)
                back = self._check(c.read_holding_registers(addr, count=1, device_id=self.unit), "read-back").registers[0]
                return f"wrote register {addr} = {q[2]}; read-back {back}"
            if kind == "write_registers":
                vals = [int(v, 0) & 0xFFFF for v in q[2].split(",")]
                self._check(c.write_registers(addr, vals, device_id=self.unit), action)
                back = self._check(c.read_holding_registers(addr, count=len(vals), device_id=self.unit), "read-back").registers
                return f"wrote registers {addr}..{addr+len(vals)-1} = {vals}; read-back {back}"
            if kind == "write_coil":
                v = q[2] in ("1", "on", "true")
                self._check(c.write_coil(addr, v, device_id=self.unit), action)
                back = self._check(c.read_coils(addr, count=1, device_id=self.unit), "read-back").bits[0]
                return f"wrote coil {addr} = {int(v)}; read-back {int(back)}"
            raise ConnectorError("unknown modbus action " + kind)
        finally:
            c.close()

    def recover(self) -> str:
        c = self._client()
        try:
            rr = c.read_holding_registers(0, count=1, device_id=self.unit)
            return "connection re-established; " + ("register 0 readable" if not rr.isError() else f"register 0: {rr}")
        finally:
            c.close()

    def docker_args(self) -> list[str]:
        return ["--device", self.port] if self.rtu and os.path.exists(self.port) else []

    def net_hosts(self) -> list[str]:
        return [] if self.rtu else [self.host]


# ---------------------------------------------------------------- HTTP

class Http:
    cls_act = CONFIG

    def __init__(self, cfg: dict):
        self.url = _f(cfg, "URL", "").rstrip("/")
        if not self.url:
            raise ConnectorError("http device needs URL=")
        auth = _f(cfg, "AUTH")
        self.auth = tuple(auth.split(":", 1)) if auth and ":" in auth else None
        self.headers = {"Authorization": f"Bearer {cfg['TOKEN']}"} if _f(cfg, "TOKEN") else {}
        self.verify = _f(cfg, "VERIFY", "1") not in ("0", "no", "false")

    def _req(self, method: str, path: str, body: str | None, timeout: float):
        import httpx
        kw: dict = {}
        if body:
            try:
                kw["json"] = json.loads(body)
            except json.JSONDecodeError:
                kw["content"] = body.encode()
        with httpx.Client(auth=self.auth, headers=self.headers, verify=self.verify, timeout=timeout, follow_redirects=True) as c:
            r = c.request(method, self.url + ("/" + path.lstrip("/") if path else ""), **kw)
        text = r.text
        if len(text) > 40_000:
            text = text[:40_000] + f"\n[... truncated, {len(r.content)} bytes]"
        return f"{method} {r.request.url} -> {r.status_code} {r.reason_phrase} ({r.headers.get('content-type','')})\n{text}"

    def observe(self, query: str, seconds: float) -> str:
        """GET /path  (or just /path); HEAD /path"""
        q = (query or "GET /").split(None, 1)
        method, path = (q[0].upper(), q[1] if len(q) > 1 else "/") if q[0].upper() in ("GET", "HEAD") else ("GET", q[0])
        return self._req(method, path, None, max(seconds, 5))

    def act(self, action: str, seconds: float) -> str:
        """POST|PUT|PATCH|DELETE /path [body]"""
        q = action.split(None, 2)
        if not q or q[0].upper() not in ("POST", "PUT", "PATCH", "DELETE"):
            raise ConnectorError("http action must be: POST|PUT|PATCH|DELETE /path [json-or-text body]")
        return self._req(q[0].upper(), q[1] if len(q) > 1 else "/", q[2] if len(q) > 2 else None, max(seconds, 10))

    actuate = act

    def recover(self) -> str:
        return self._req("GET", "/", None, 10)

    def docker_args(self) -> list[str]:
        return []

    def net_hosts(self) -> list[str]:
        from urllib.parse import urlparse
        return [urlparse(self.url).hostname or ""]


# ---------------------------------------------------------------- factory

def make(cfg: dict):
    t = (cfg.get("TYPE") or "ssh").lower()
    if t == "serial":
        return Serial(cfg)
    if t == "can":
        return Can(cfg)
    if t == "modbus":
        return Modbus(cfg)
    if t == "http":
        return Http(cfg)
    if t == "ssh":
        return None          # handled natively by target_host.py (async SSH)
    raise ConnectorError(f"unknown device TYPE '{t}' (known: {', '.join(TYPES)})")


def describe(cfg: dict) -> str:
    t = (cfg.get("TYPE") or "ssh").lower()
    if t == "serial":
        return f"serial {cfg.get('PORT', '/dev/ttyUSB0')} @ {cfg.get('BAUD', 115200)}"
    if t == "can":
        return f"can {cfg.get('IFACE', 'can0')} @ {cfg.get('BITRATE', 500000)}"
    if t == "modbus":
        p = cfg.get("PORT", "502")
        return f"modbus-rtu {p} @ {cfg.get('BAUD', 9600)} unit {cfg.get('UNIT', 1)}" if str(p).startswith("/dev/") \
            else f"modbus-tcp {cfg.get('HOST')}:{p} unit {cfg.get('UNIT', 1)}"
    if t == "http":
        return f"http {cfg.get('URL')}"
    return f"ssh {cfg.get('USER', 'support-agent')}@{cfg.get('HOST', 'auto')}:{cfg.get('PORT', 22)}" \
        + (f" (mdns={cfg['MDNS']})" if cfg.get("MDNS") else "") + (f" (mac={cfg['MAC']})" if cfg.get("MAC") else "")


def quote(s: str) -> str:
    return shlex.quote(s)


def load_env(path: Path) -> dict:
    cfg: dict = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"')
    return cfg
