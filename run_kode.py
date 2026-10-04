"""K.O.D.E. launcher.

  python run_kode.py                      Brain + console on http://localhost:8000/console  (opens the browser)
  python run_kode.py --demo-nodes 3       ... and start 3 real node agents on THIS PC with real provider sessions
  python run_kode.py --lan                listen on all interfaces so teammates on the same network can join
  python run_kode.py --public-url URL     teammates elsewhere: put a tunnel in front of port 8000 (e.g. cloudflared) and pass its URL
  python run_kode.py --simulation         the old timer-driven mock fleet UI (badged SIMULATION), no engine calls

The classic engine UI (feature -> 3 agents -> PRs) stays available at /.
"""
import argparse
import asyncio
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)                                   # relative paths in .env (db, contracts) resolve here

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8000)
ap.add_argument("--lan", action="store_true", help="bind 0.0.0.0 (other computers can reach the Brain)")
ap.add_argument("--public-url", default="", help="the URL teammates use to reach this Brain, e.g. a tunnel: https://name.trycloudflare.com")
ap.add_argument("--simulation", action="store_true", help="SIMULATION_MODE=1: mock fleet only, no engine/LLM/GitHub")
ap.add_argument("--demo-nodes", type=int, default=0, help="start N real kode-node agents on this PC with real provider sessions")
ap.add_argument("--no-browser", action="store_true")
args = ap.parse_args()

from config import Settings                          # noqa: E402

overrides = {"kode_enable_hub": True, "kode_frontend_dir": str(ROOT)}
if args.simulation:
    overrides["simulation_mode"] = True
settings = Settings(**overrides)
mode = "SIMULATION (mock fleet, badged in the classic UI)" if settings.simulation_mode else "BRAIN + ENGINE"
host = "0.0.0.0" if args.lan else "127.0.0.1"
os.environ["KODE_BIND_HOST"], os.environ["KODE_PORT"] = host, str(args.port)
if args.public_url:
    os.environ["KODE_PUBLIC_URL"] = args.public_url.rstrip("/")
print(f"\n>>> K.O.D.E. mode: {mode}\n>>> console:  http://localhost:{args.port}/console\n>>> classic:  http://localhost:{args.port}/\n"
      + (">>> LAN mode: anyone on this network can reach the pairing endpoint; pairing still needs a one-time code.\n" if args.lan else ""), flush=True)

from app import create_app                           # noqa: E402
import uvicorn                                       # noqa: E402

procs = []


def after_start():
    import httpx
    for _ in range(150):
        try:
            if httpx.get(f"http://127.0.0.1:{args.port}/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.2)
    if args.demo_nodes:
        sys.path.insert(0, str(BACKEND / "scripts"))
        import demo_fleet
        key = (BACKEND / ".hub_key").read_text().strip()
        print(">>> starting demo fleet (real node processes on this PC):", flush=True)
        try:
            procs.extend(asyncio.run(demo_fleet.setup(f"http://127.0.0.1:{args.port}", key, args.demo_nodes, log=lambda m: print(m, flush=True))))
        except Exception as e:
            print(">>> demo fleet failed:", type(e).__name__, e, flush=True)
    if not args.no_browser:
        webbrowser.open(f"http://localhost:{args.port}/console")


threading.Thread(target=after_start, daemon=True).start()
try:
    uvicorn.run(create_app(settings), host=host, port=args.port, log_level="warning")
finally:
    for p in procs:
        p.terminate()
