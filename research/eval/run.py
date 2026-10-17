#!/usr/bin/env python3
"""Research-chat eval: the same tasks, tools and tool loop for each local LLM profile, scored automatically.

For every task in tasks.json: create a chat, send each turn through POST /api/chats/{id}/send (streaming SSE),
score the final answer, the source ledger (GET /api/chats/{id}/sources) and the tool results the model actually
received (GET /api/chats/{id}), then delete the chat. Per model the run writes <out>/<stamp>-<profile>.json and
.md; report.md in <out> puts the latest run of every model side by side (task x model).

Models run one after another, never in parallel (one GPU): with --switch the runner loads each profile through
POST /api/models/{profile} (5-10 min each; this unloads whatever is running) and afterwards loads the original one
again unless --no-restore.

    python eval/run.py                                   # the model that is loaded now
    python eval/run.py --models fast deep nemotron --switch
    python eval/run.py --tasks attn_schedule,primes_sum  # a subset
    python eval/run.py report                            # rebuild report.md from the saved runs

Needs only the stdlib and httpx (the chat's venv has it: /opt/research/venv/bin/python on the box).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

HERE = Path(__file__).parent
STOPPED = "_(stopped: too many tool steps)_"
SOURCE_HEAD = re.compile(r"^S(\d+) (·|from offset)")  # sources.read() output starts with "S3 · title" / "S3 from offset"


def log(*a):
    print(f"[{datetime.now():%H:%M:%S}]", *a, file=sys.stderr, flush=True)


def norm(s: str) -> str:
    return " ".join(s.split()).lower()


class Chat:
    """Thin client of the research-chat HTTP API."""

    def __init__(self, base: str, password: str = ""):
        auth = ("research", password) if password else None
        self.c = httpx.Client(base_url=base.rstrip("/"), auth=auth,
                              timeout=httpx.Timeout(30, read=1500))  # one machine tool call may run 16 min silently

    def get(self, path: str, **params):
        r = self.c.get(path, params=params)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict | None = None):
        r = self.c.post(path, json=body or {})
        if r.status_code >= 400:
            raise RuntimeError(f"POST {path}: HTTP {r.status_code} {r.text[:300]}")
        return r.json()

    def delete(self, path: str):
        self.c.delete(path).raise_for_status()

    def send(self, cid: str, body: dict, limit: float) -> list[dict]:
        """One turn; returns the SSE events in order. After `limit` seconds the stream is closed (the chat then
        stops the generation): a model stuck in a reasoning loop keeps streaming and never hits a read timeout."""
        events, t0 = [], time.time()
        with self.c.stream("POST", f"/api/chats/{cid}/send", json=body) as r:
            if r.status_code >= 400:
                r.read()
                return [{"type": "error", "message": f"HTTP {r.status_code}: {r.text[:300]}"}]
            for line in r.iter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
                if time.time() - t0 > limit:
                    events.append({"type": "timeout", "message": f"turn cut after {limit:.0f} s"})
                    break
        return events


# ---------- models ----------

def current_profile(api: Chat) -> tuple[str, dict | None]:
    m = api.get("/api/models")
    loaded = m.get("loaded")
    if not loaded:
        return "", None
    for p in m["profiles"]:
        if loaded["id"] == p["profile"] or loaded["root"] == p["dir"]:
            return p["profile"], loaded
    return "", loaded


def ensure_model(api: Chat, prof: str, switch: bool, wait_min: float = 25):
    have, _ = current_profile(api)
    if have == prof:
        return
    if not switch:
        sys.exit(f"profile '{prof}' is not loaded (now: '{have or 'none'}'); pass --switch to load it "
                 "(this unloads the running model)")
    log(f"loading {prof} (unloads {have or 'nothing'}) ...")
    api.post(f"/api/models/{prof}")
    t0 = time.time()
    while time.time() - t0 < wait_min * 60:
        time.sleep(15)
        try:
            have, _ = current_profile(api)
            state = next(p["state"] for p in api.get("/api/models")["profiles"] if p["profile"] == prof)
        except httpx.HTTPError:
            continue
        if have == prof:
            log(f"{prof} loaded after {time.time() - t0:.0f} s")
            return
        if state == "failed":
            sys.exit(f"llm@{prof} failed to start (journalctl -u llm@{prof} on the box)")
    sys.exit(f"{prof} not loaded after {wait_min} min")


def tool_stats(api: Chat) -> dict:
    return {(r["model"], r["name"]): r for r in api.get("/api/tool-stats", days=2)}


def stats_delta(before: dict, after: dict, model: str) -> list[dict]:
    keys = ("calls", "errors", "repeats", "bad_args", "not_offered")
    out = []
    for (m, name), r in sorted(after.items()):
        if m != model:
            continue
        b = before.get((m, name), {})
        d = {k: (r[k] or 0) - (b.get(k) or 0) for k in keys}
        if d["calls"]:
            out.append({"tool": name, **d, "avg_ms": r["avg_ms"]})
    return out


# ---------- scoring ----------

def turns_of(messages: list[dict]) -> list[list[dict]]:
    """Stored chat messages split into turns (each starts with a user message)."""
    turns: list[list[dict]] = []
    for m in messages:
        if m["role"] == "user":
            turns.append([])
        if turns:
            turns[-1].append(m)
    return turns


def pattern(p: str, named: dict) -> str:
    return named.get(p[1:], p) if p.startswith("@") else p


def check(spec: dict, answer: str, srcs: list[dict], turn: list[dict], used: set[str], named: dict) -> list[dict]:
    out = []
    answer = answer.replace("*", "")  # markdown emphasis splits phrases: "does **not** report"
    for p in spec.get("answer", []):
        ok = re.search(pattern(p, named), answer, re.I | re.S) is not None
        out.append({"check": f"answer ~ {p}", "ok": ok})
    for p in spec.get("answer_not", []):
        ok = re.search(pattern(p, named), answer, re.I | re.S) is None
        out.append({"check": f"answer !~ {p}", "ok": ok})
    for p in spec.get("sources", []):
        hit = [s for s in srcs if re.search(p, f"{s['ref']} {s['url']}", re.I)]
        out.append({"check": f"source ~ {p}", "ok": bool(hit),
                    "detail": "; ".join(f"S{s['n']} {s['ref']} read {s['coverage']}" for s in hit)})
    by_n = {s["n"]: s for s in srcs}
    for r in spec.get("read", []):
        hit = ""
        for m in turn:
            if m["role"] != "tool":
                continue
            n = m.get("source")
            if n is None and m.get("name") == "source_read" and (h := SOURCE_HEAD.match(m["content"])):
                n = int(h.group(1))
            s = by_n.get(n)
            if s and re.search(r["source"], f"{s['ref']} {s['url']}", re.I) and norm(r["anchor"]) in norm(m["content"]):
                hit = f"via {m['name']} (S{n}, read {s['coverage']})"
                break
        out.append({"check": f"read '{r['anchor']}' in {r['source']}", "ok": bool(hit), "detail": hit})
    for group in spec.get("tools", []):
        out.append({"check": f"used {' | '.join(group)}", "ok": bool(used & set(group))})
    return out


def run_task(api: Chat, task: dict, named: dict, reasoning: str, keep: bool, limit: float) -> dict:
    cid = api.post("/api/chats")["id"]
    res = {"id": task["id"], "kind": task.get("kind", ""), "chat": cid, "turns": []}
    t_task = time.time()
    try:
        for i, t in enumerate(task["turns"]):
            t0 = time.time()
            events = api.send(cid, {"text": t["text"], "tools": task.get("tools", True),
                                    "machine": task.get("machine", False), "reasoning": reasoning}, limit)
            wall = time.time() - t0
            chat = api.get(f"/api/chats/{cid}")
            turn = turns_of(chat["messages"])[-1]
            srcs = chat["sources"]
            assistant = [m for m in turn if m["role"] == "assistant"]
            answer = next((m["content"] for m in reversed(assistant) if m.get("content")), "")
            results = [e for e in events if e["type"] == "tool_result"]
            used = {e["name"] for e in events if e["type"] == "tool"}
            usage = [e for e in events if e["type"] == "usage"]
            errors = [e["message"] for e in events if e["type"] == "error"]
            stopped = any(e["type"] == "text" and STOPPED in e["text"] for e in events)
            timeout = next((e["message"] for e in events if e["type"] == "timeout"), "")
            checks = check(t.get("expect", {}), answer, srcs, turn, used, named)
            soft = check(t.get("soft", {}), answer, srcs, turn, used, named)
            if errors:
                checks.append({"check": "no stream error", "ok": False, "detail": errors[0][:300]})
            if stopped:
                checks.append({"check": "within step limit", "ok": False})
            if timeout:
                checks.append({"check": "within turn time limit", "ok": False, "detail": timeout})
            if not answer:
                checks.append({"check": "non-empty answer", "ok": False})
            res["turns"].append({
                "turn": i + 1, "ok": all(c["ok"] for c in checks), "checks": checks, "soft": soft,
                "steps": len(assistant), "tool_calls": len(results),
                "tool_errors": sum(e["content"].startswith("ERROR") for e in results),
                "repeats": sum(e["content"].startswith("NOT RUN") for e in results),
                "tools": [e["name"] for e in events if e["type"] == "tool"],
                "completion_tokens": sum(u.get("completion_tokens") or 0 for u in usage),
                "prompt_tokens": max((u.get("prompt_tokens") or 0 for u in usage), default=0),
                "wall_s": round(wall, 1), "answer": answer, "sources": srcs,
                "model": next((e["model"] for e in events if e["type"] == "model"), "")})
    except Exception as e:  # a broken task must not end the run
        res["exception"] = f"{type(e).__name__}: {e}"
    finally:
        if not keep:
            try:
                api.delete(f"/api/chats/{cid}")
            except httpx.HTTPError:
                pass
    tr = res["turns"]
    res.update({"ok": bool(tr) and len(tr) == len(task["turns"]) and all(t["ok"] for t in tr) and "exception" not in res,
                "soft_ok": all(c["ok"] for t in tr for c in t["soft"]),
                "steps": sum(t["steps"] for t in tr), "tool_calls": sum(t["tool_calls"] for t in tr),
                "tool_errors": sum(t["tool_errors"] for t in tr), "repeats": sum(t["repeats"] for t in tr),
                "completion_tokens": sum(t["completion_tokens"] for t in tr),
                "wall_s": round(time.time() - t_task, 1)})
    return res


# ---------- reports ----------

def cell(t: dict | None) -> str:
    if not t:
        return "–"
    mark = "**PASS**" if t["ok"] else "FAIL"
    if t["ok"] and not t["soft_ok"]:
        mark += "\\*"
    return (f"{mark} · {t['steps']} st · {t['tool_calls']} calls · {t['tool_errors']} err · {t['repeats']} rep · "
            f"{t['wall_s']:.0f} s")


def model_section(run: dict) -> list[str]:
    out = [f"\n## {run['profile']} — {run['model']}\n",
           f"Run {run['started']} · reasoning `{run['reasoning']}` · {run['passed']}/{len(run['tasks'])} passed · "
           f"{run['wall_s'] / 60:.1f} min · {run['completion_tokens']} completion tokens"
           + (f" · note: {run['note']}" if run.get("note") else "") + "\n"]
    for t in run["tasks"]:
        bad = [f"`{c['check']}`" + (f" ({c['detail']})" if c.get("detail") else "")
               for tr in t["turns"] for c in tr["checks"] if not c["ok"]]
        softbad = [f"`{c['check']}`" for tr in t["turns"] for c in tr["soft"] if not c["ok"]]
        tools = " ‖ ".join(" → ".join(tr["tools"]) or "none" for tr in t["turns"])
        out.append(f"- **{t['id']}** {'pass' if t['ok'] else 'FAIL'}"
                   + (f" — failed: {', '.join(bad)}" if bad else "")
                   + (f" — soft: {', '.join(softbad)}" if softbad else "")
                   + (f" — exception: {t['exception']}" if t.get("exception") else "")
                   + f"\n  - tools: {tools or 'none'}")
        for tr in t["turns"]:
            a = " ".join(tr["answer"].split())
            which = f" (turn {tr['turn']})" if len(t["turns"]) > 1 else ""
            out.append(f"  - answer{which}: {a[:300]}{'…' if len(a) > 300 else ''}")
    if run.get("tool_stats"):
        out += ["\n/api/tool-stats delta for this run (includes any other chat on this model meanwhile):\n",
                "| tool | calls | errors | repeats | bad_args | not_offered | avg ms (2 d) |", "|---|---|---|---|---|---|---|"]
        out += [f"| {r['tool']} | {r['calls']} | {r['errors']} | {r['repeats']} | {r['bad_args']} | {r['not_offered']} | "
                f"{r['avg_ms']} |" for r in run["tool_stats"]]
    return out


def write_report(out_dir: Path, tasks: list[dict]) -> Path:
    runs: dict[str, dict] = {}
    for f in sorted(out_dir.glob("*.json")):  # names start with a timestamp: the last one per model wins
        if f.name.endswith(".partial.json"):
            continue
        r = json.loads(f.read_text())
        runs[r["profile"]] = r
    order = [p for p in ("fast", "deep", "nemotron") if p in runs] + [p for p in runs if p not in ("fast", "deep", "nemotron")]
    lines = ["# Research-chat eval\n",
             f"Generated {datetime.now():%Y-%m-%d %H:%M} from the latest run of each model in `{out_dir}`. "
             "Cell: result · LLM steps · tool calls · tool errors · repeated calls (answered without running) · wall time. "
             "PASS\\* = passed, a soft check (e.g. used source_read) failed.\n",
             "| task | kind | " + " | ".join(f"{p} ({runs[p]['model']})" for p in order) + " |",
             "|---|---|" + "---|" * len(order)]
    for task in tasks:
        row = [next((t for t in runs[p]["tasks"] if t["id"] == task["id"]), None) for p in order]
        lines.append(f"| {task['id']} | {task.get('kind', '')} | " + " | ".join(cell(t) for t in row) + " |")
    lines.append("| **total** | | " + " | ".join(
        f"**{runs[p]['passed']}/{len(runs[p]['tasks'])}** · {sum(t['steps'] for t in runs[p]['tasks'])} st · "
        f"{sum(t['tool_errors'] for t in runs[p]['tasks'])} err · {sum(t['repeats'] for t in runs[p]['tasks'])} rep · "
        f"{runs[p]['wall_s'] / 60:.0f} min" for p in order) + " |")
    for p in order:
        lines += model_section(runs[p])
    path = out_dir / "report.md"
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="run", choices=["run", "report"])
    ap.add_argument("--base", default="http://127.0.0.1:8090", help="chat URL (default: on the box)")
    ap.add_argument("--password", default=os.environ.get("RESEARCH_PASSWORD", ""), help="basic auth, for non-local URLs")
    ap.add_argument("--models", nargs="*", help="profiles to run in this order (default: the loaded one)")
    ap.add_argument("--switch", action="store_true", help="allow loading other profiles (unloads the running model)")
    ap.add_argument("--no-restore", action="store_true", help="after switching, leave the last profile loaded")
    ap.add_argument("--tasks", default="", help="comma-separated task ids (default: all)")
    ap.add_argument("--reasoning", default="medium", help="off | low | medium | high (as the chat's reasoning menu)")
    ap.add_argument("--file", default=str(HERE / "tasks.json"))
    ap.add_argument("--out", default=os.path.expanduser("~/research-eval"),
                    help="results folder (keep it outside the deployed tree: deploys rsync --delete)")
    ap.add_argument("--keep", action="store_true", help="keep the eval chats (for reading them in the UI)")
    ap.add_argument("--turn-timeout", type=float, default=900, help="seconds per turn before the stream is cut")
    args = ap.parse_args()

    spec = json.loads(Path(args.file).read_text())
    tasks, named = spec["tasks"], spec.get("patterns", {})
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.command == "report":
        print(write_report(out_dir, tasks))
        return
    if args.tasks:
        want = args.tasks.split(",")
        unknown = set(want) - {t["id"] for t in tasks}
        if unknown:
            sys.exit(f"unknown task(s): {', '.join(sorted(unknown))}")
        tasks_run = [t for t in tasks if t["id"] in want]
    else:
        tasks_run = tasks

    api = Chat(args.base, args.password)
    original, _ = current_profile(api)
    models = args.models or [original]
    if not models[0]:
        sys.exit("no model is loaded; pass --models <profile> --switch")
    try:
        for prof in models:
            ensure_model(api, prof, args.switch)
            _, loaded = current_profile(api)
            started, t0 = datetime.now(), time.time()
            before = tool_stats(api)
            stem = out_dir / f"{started:%Y%m%d-%H%M}-{prof}"

            def save(done: list[dict]):  # after every task: an interrupted run keeps what it measured
                (stem.parent / (stem.name + ".partial.json")).write_text(json.dumps(
                    {"profile": prof, "model": loaded["root"], "started": f"{started:%Y-%m-%d %H:%M}", "tasks": done},
                    indent=1, ensure_ascii=False))

            log(f"== {prof} ({loaded['root']}): {len(tasks_run)} tasks")
            done = []
            for task in tasks_run:
                r = run_task(api, task, named, args.reasoning, args.keep, args.turn_timeout)
                log(f"{task['id']:<18} {'pass' if r['ok'] else 'FAIL'}  {r['steps']} steps  {r['tool_calls']} calls  "
                    f"{r['tool_errors']} err  {r['repeats']} rep  {r['wall_s']:.0f} s" + (f"  {r['exception']}" if r.get("exception") else ""))
                done.append(r)
                save(done)
            run = {"profile": prof, "model": loaded["root"], "started": f"{started:%Y-%m-%d %H:%M}",
                   "reasoning": args.reasoning, "base": args.base, "passed": sum(t["ok"] for t in done),
                   "wall_s": round(time.time() - t0, 1), "completion_tokens": sum(t["completion_tokens"] for t in done),
                   "tool_stats": stats_delta(before, tool_stats(api), loaded["root"]), "tasks": done}
            (stem.parent / (stem.name + ".partial.json")).unlink(missing_ok=True)
            stem.with_suffix(".json").write_text(json.dumps(run, indent=1, ensure_ascii=False))
            stem.with_suffix(".md").write_text("\n".join(model_section(run)) + "\n")
            log(f"{prof}: {run['passed']}/{len(done)} passed -> {stem}.json")
    finally:
        if args.switch and original and not args.no_restore and current_profile(api)[0] != original:
            ensure_model(api, original, True)
    print(write_report(out_dir, tasks))


if __name__ == "__main__":
    main()
