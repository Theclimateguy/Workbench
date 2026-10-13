#!/usr/bin/env python3
"""Minimal OpenAI-API benchmark for the local LLM endpoint. Stdlib only.

Measures: TTFT (streaming), single-stream decode tok/s, aggregate tok/s at N
concurrent streams, and a tool-call sanity check.

  python3 bench.py --url http://127.0.0.1:8000 --model fast --concurrency 8
"""
import argparse
import json
import sys
import threading
import time
import urllib.request

PROMPT_SHORT = "Explain in ~300 words how systemd orders units at boot."
PROMPT_LONG_PREFIX = ("You are a Linux support engineer. Reference notes:\n" +
                      ("- netplan renders to systemd-networkd or NetworkManager.\n" * 400))

TOOLS = [{
    "type": "function",
    "function": {
        "name": "run_readonly",
        "description": "Run a read-only shell command on the target host and return stdout.",
        "parameters": {
            "type": "object",
            "properties": {"cmd": {"type": "string", "description": "shell command"}},
            "required": ["cmd"],
        },
    },
}]


def post(url, body, timeout=600):
    req = urllib.request.Request(
        url + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    return urllib.request.urlopen(req, timeout=timeout)


def one_completion(url, model, prompt, max_tokens, stream=False):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": stream,
    }
    if stream:
        body["stream_options"] = {"include_usage": True}
    t0 = time.time()
    resp = post(url, body)
    if not stream:
        data = json.load(resp)
        dt = time.time() - t0
        n = data["usage"]["completion_tokens"]
        return {"tokens": n, "seconds": dt, "tps": n / dt, "ttft": None,
                "prompt_tokens": data["usage"]["prompt_tokens"]}
    ttft = None
    n = 0
    usage = None
    for line in resp:
        line = line.decode().strip()
        if not line.startswith("data:") or line == "data: [DONE]":
            continue
        chunk = json.loads(line[5:])
        if chunk.get("usage"):
            usage = chunk["usage"]
        if not chunk.get("choices"):
            continue
        delta = chunk["choices"][0].get("delta", {})
        if delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning"):
            if ttft is None:
                ttft = time.time() - t0
            n += 1
    dt = time.time() - t0
    # With speculative decoding a chunk may carry several tokens: trust usage, not chunk count.
    if usage:
        n = usage["completion_tokens"]
    return {"tokens": n, "seconds": dt, "tps": n / dt if dt else 0, "ttft": ttft, "prompt_tokens": None}


def content_check(url, model):
    """Plain chat answer must carry text in `content` with reasoning kept apart (`reasoning_content`).
    Catches the gpt-oss/Harmony failure mode on SM121 where the first token is wrong and content comes back null."""
    body = {"model": model, "messages": [{"role": "user", "content": "Ответь одной короткой фразой: что такое systemd?"}],
            "max_tokens": 400, "temperature": 0}
    data = json.load(post(url, body))
    msg = data["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or msg.get("reasoning") or "").strip()
    finish = data["choices"][0].get("finish_reason")
    ok = bool(content) and finish == "stop"
    return ok, content[:80], len(reasoning), finish


def tool_call_check(url, model):
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a support agent. Use tools to inspect the host before answering."},
            {"role": "user", "content": "Which systemd units have failed on the target host?"},
        ],
        "tools": TOOLS,
        "tool_choice": "auto",
        "max_tokens": 512,
        "temperature": 0,
    }
    data = json.load(post(url, body))
    msg = data["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    ok = bool(calls) and calls[0]["function"]["name"] == "run_readonly"
    args = calls[0]["function"]["arguments"] if calls else msg.get("content")
    return ok, args


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--model", default="fast")
    ap.add_argument("--max-tokens", type=int, default=400)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--skip-concurrency", action="store_true")
    a = ap.parse_args()

    print(f"endpoint={a.url} model={a.model}")
    one_completion(a.url, a.model, "warmup", 32)  # warm-up, discarded

    r = one_completion(a.url, a.model, PROMPT_SHORT, a.max_tokens, stream=True)
    print(f"single-stream short: {r['tokens']} tok in {r['seconds']:.1f}s = {r['tps']:.1f} tok/s, TTFT {r['ttft']*1000:.0f} ms")

    r = one_completion(a.url, a.model, PROMPT_LONG_PREFIX + PROMPT_SHORT, a.max_tokens)
    print(f"long prompt ({r['prompt_tokens']} tok) cold: {r['tps']:.1f} tok/s, total {r['seconds']:.1f}s")
    r = one_completion(a.url, a.model, PROMPT_LONG_PREFIX + "Now explain cron vs systemd timers.", a.max_tokens)
    print(f"long prompt prefix-cached: {r['tps']:.1f} tok/s, total {r['seconds']:.1f}s")

    if not a.skip_concurrency:
        results = []
        lock = threading.Lock()

        def worker(i):
            rr = one_completion(a.url, a.model, f"{PROMPT_SHORT} (variant {i})", a.max_tokens)
            with lock:
                results.append(rr)

        t0 = time.time()
        ths = [threading.Thread(target=worker, args=(i,)) for i in range(a.concurrency)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        dt = time.time() - t0
        tot = sum(x["tokens"] for x in results)
        print(f"{a.concurrency} concurrent: {tot} tok in {dt:.1f}s = {tot/dt:.1f} tok/s aggregate, "
              f"{tot/dt/a.concurrency:.1f} tok/s per stream")

    c_ok, preview, rlen, finish = content_check(a.url, a.model)
    print(f"content: {'OK' if c_ok else 'FAIL'} (finish={finish}, reasoning {rlen} chars) -> {preview!r}")
    ok, args = tool_call_check(a.url, a.model)
    print(f"tool-call: {'OK' if ok else 'FAIL'} -> {args}")
    sys.exit(0 if ok and c_ok else 1)


if __name__ == "__main__":
    main()
