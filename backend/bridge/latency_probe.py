"""P0 gate: measure keystroke round-trip (client send -> echoed output back) over the real WebSocket path.

  python -m bridge.latency_probe [--port 8000] [--provider shell] [--n 60]
Needs the server running with KODE_ENABLE_BRIDGE=1. Prints median/p95/max in ms and PASS/FAIL against --gate (default 150).
"""
import argparse
import asyncio
import json
import re
import statistics
import sys
import time

import httpx
import websockets


async def probe(port: int, provider: str, n: int):
    base = f"http://127.0.0.1:{port}"
    html = httpx.get(base + "/spike", timeout=10).text
    token = re.search(r'const TOKEN = "([^"]+)"', html).group(1)
    uri = f"ws://127.0.0.1:{port}/spike/ws?provider={provider}&token={token}&cols=100&rows=30"
    rtts = []
    async with websockets.connect(uri, max_size=None) as ws:
        hello = json.loads(await ws.recv())
        assert hello["t"] == "hello", hello
        await asyncio.sleep(1.5)                                   # let the shell print its banner/prompt
        while True:                                                # drain startup output
            try:
                await asyncio.wait_for(ws.recv(), 0.4)
            except asyncio.TimeoutError:
                break
        for i in range(n):
            ch = "abcdefghijklmnopqrstuvwxyz"[i % 26]
            t0 = time.perf_counter()
            await ws.send(json.dumps({"t": "in", "d": ch}))
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), 5))
                if m["t"] == "out" and ch in m["d"]:
                    rtts.append((time.perf_counter() - t0) * 1000)
                    break
            await asyncio.sleep(0.03)
        await ws.send(json.dumps({"t": "in", "d": "\x15exit\r"}))   # clear line, exit
    return rtts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--provider", default="shell")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--gate", type=float, default=150.0)
    a = ap.parse_args()
    r = asyncio.run(probe(a.port, a.provider, a.n))
    r_sorted = sorted(r)
    p95 = r_sorted[int(0.95 * len(r_sorted)) - 1]
    print(f"keystroke round-trip over WS+ConPTY: n={len(r)} median={statistics.median(r):.1f}ms p95={p95:.1f}ms max={max(r):.1f}ms@idx{r.index(max(r))} (gate < {a.gate:.0f}ms)")
    ok = statistics.median(r) < a.gate and p95 < a.gate
    print("P0 LATENCY GATE:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
