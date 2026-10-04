#!/usr/bin/env python3
"""Smoke test for the target-host MCP server: lists tools, runs read-only calls against a
target, and checks that mutating commands are rejected by run_readonly.

  /opt/support/venv/bin/python mcp_smoke.py [--target self]
"""
import argparse
import asyncio
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = os.environ.get("TARGET_HOST_MCP", "/opt/support/mcp/target_host.py")
PYTHON = os.environ.get("TARGET_HOST_PY", "/opt/support/venv/bin/python")


async def main(target: str) -> int:
    params = StdioServerParameters(command=PYTHON, args=[SERVER], env={**os.environ})
    failures = 0
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = sorted(t.name for t in (await s.list_tools()).tools)
            print("tools:", ", ".join(tools))
            expected = {"target_list", "collect_facts", "run_readonly", "read_file",
                        "run_change", "write_file", "reboot_and_wait"}
            if not expected <= set(tools):
                print("MISSING tools:", expected - set(tools)); failures += 1

            async def call(name, **kw):
                res = await s.call_tool(name, kw)
                return "".join(c.text for c in res.content if hasattr(c, "text"))

            out = await call("target_list")
            print("target_list:", out.strip().splitlines()[0] if out.strip() else out)

            out = await call("run_readonly", cmd="systemctl --failed --no-legend | wc -l", target=target)
            print("run_readonly ok:", out.strip().replace("\n", " | ")[:80])
            if not out.startswith("rc=0"):
                failures += 1

            for bad in ["systemctl restart ssh", "rm -rf /tmp/x", "cat /etc/hosts > /tmp/h", "apt install foo",
                        "ip link set enP7s7 down", "echo hi; reboot", "sudo systemctl stop docker"]:
                out = await call("run_readonly", cmd=bad, target=target)
                ok = out.startswith("REJECTED")
                print(f"reject {'OK ' if ok else 'FAIL'}: {bad!r} -> {out.splitlines()[0][:70]}")
                failures += 0 if ok else 1

            out = await call("read_file", path="/etc/hostname", target=target)
            print("read_file:", out.strip()[:40])

            out = await call("collect_facts", target=target)
            print("collect_facts:", len(out), "bytes;", "### failed units" in out)
            if "### failed units" not in out:
                failures += 1
    print("FAILURES:", failures)
    return failures


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="self")
    raise SystemExit(asyncio.run(main(ap.parse_args().target)))
