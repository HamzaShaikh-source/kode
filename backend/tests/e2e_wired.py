"""T3.5 - REAL end-to-end through the UI. Not collected by pytest (needs network, real LLM + GitHub).

  python tests/e2e_wired.py

Boots the real app (fresh DB) -> headless Chrome opens the UI -> types the demo prompt -> real LLM plans,
freezes the contract, 3 agents run in parallel -> 3 real PRs appear in the UI via SSE -> click Approve on one,
Reject on another -> GitHub REST API proves exactly one merge and main unchanged by the rejection.
"""
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx                                              # noqa: E402
import uvicorn                                            # noqa: E402
from playwright.sync_api import sync_playwright           # noqa: E402

from app import create_app                                # noqa: E402
from config import Settings                               # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
R = []


def check(name, ok, detail=""):
    R.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)


tmp = tempfile.mkdtemp(prefix="kode_e2e_")
s = Settings(kode_db_path=str(Path(tmp) / "e2e.db"), kode_frontend_dir=str(ROOT))
sk = socket.socket(); sk.bind(("127.0.0.1", 0)); port = sk.getsockname()[1]; sk.close()
server = uvicorn.Server(uvicorn.Config(create_app(s), host="127.0.0.1", port=port, log_level="error"))
threading.Thread(target=server.run, daemon=True).start()
BASE = f"http://127.0.0.1:{port}"
for _ in range(100):
    try:
        if httpx.get(BASE + "/health").status_code == 200:
            break
    except httpx.HTTPError:
        time.sleep(0.1)

GH = {"Authorization": "Bearer " + s.github_token.get_secret_value(), "Accept": "application/vnd.github+json"}
gh = lambda p: httpx.get(f"https://api.github.com/repos/{s.github_repo}{p}", headers=GH, timeout=30).json()
main_sha = lambda: gh("/branches/main")["commit"]["sha"]

with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    pg = b.new_context(viewport={"width": 1440, "height": 900}).new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.goto(BASE + "/#/overview")
    pg.wait_for_selector("#fr-prompt", timeout=15000)
    check("UI served by the engine, ENGINE mode (no SIMULATION ribbon)", pg.is_hidden("#mode-ribbon") and "engine" in pg.inner_text("#sim-pill").lower(), pg.inner_text("#sim-pill"))
    check("starts empty: no seeded fake data", pg.evaluate("window.__kode.getState().tasks.length") == 0)
    pg.wait_for_function("window.__kode.engine.connected === true", timeout=10000)
    check("EventSource connected", True)

    main0 = main_sha()
    t0 = time.time()
    pg.click("#fr-run")
    pg.wait_for_selector("text=Frozen contract", timeout=240000)
    check("POST /features -> plan + frozen contract shown", True, f"{time.time()-t0:.0f}s: " + pg.inner_text("[data-region=ov-contract] p").replace("\n", " ")[:90])
    saw_working = False
    deadline = time.time() + 420
    while time.time() < deadline:
        if pg.locator("#roster li.s-working").count():
            saw_working = True
        if pg.locator("button[data-act=appr-approve]").count() >= 3:
            break
        time.sleep(1.0)
    check("agents shown WORKING live while running", saw_working)
    n_appr = pg.locator("button[data-act=appr-approve]").count()
    check("3 PR approvals surfaced in the UI via SSE", n_appr == 3, f"{n_appr} pending in {time.time()-t0:.0f}s")
    types = pg.evaluate("[...new Set(window.__kode.getState().log.map(e => e.msg.split(']')[0].slice(1)))]")
    check("all engine transition types observed", {"THINKING", "WRITE_CODE", "COMMIT", "PR_CREATED", "NEEDS_REVIEW"} <= set(types), str(sorted(types)))
    check("main untouched until a human decides", main_sha() == main0)
    appr = {a["role"]: a for a in httpx.get(BASE + "/approvals").json()}
    print("      PRs:", {r: a["pr"]["url"] for r, a in appr.items()}, flush=True)
    authors = {a["commit"]["author_email"] for a in appr.values()}
    check("3 distinct synthetic commit authors", authors == {"naman@bugged.dev", "nishitha@bugged.dev", "jyoti@bugged.dev"}, str(sorted(authors)))

    # reject QA via UI
    pg.click(f'button[aria-label="Reject qa PR #{appr["qa"]["pr"]["number"]}"]')
    pg.wait_for_selector(f'li.appr:has-text("PR #{appr["qa"]["pr"]["number"]}") >> text=Rejected', timeout=60000)
    qa = gh(f"/pulls/{appr['qa']['pr']['number']}")
    check("UI reject: PR closed, NOT merged, main unchanged", qa["state"] == "closed" and not qa["merged"] and main_sha() == main0)

    # approve backend via UI
    pg.click(f'button[aria-label="Approve and merge backend PR #{appr["backend"]["pr"]["number"]}"]')
    pg.wait_for_selector(f'li.appr:has-text("PR #{appr["backend"]["pr"]["number"]}") >> text=Merged', timeout=90000)
    be = gh(f"/pulls/{appr['backend']['pr']['number']}")
    fe = gh(f"/pulls/{appr['frontend']['pr']['number']}")
    check("UI approve: backend PR merged on GitHub", be["merged"] is True)
    check("exactly one merge; frontend PR still open", fe["state"] == "open" and not fe["merged"] and not qa["merged"])
    check("main advanced exactly by the approved merge", main_sha() != main0 and main_sha() == be["merge_commit_sha"])
    pg.goto(BASE + "/#/tasks"); pg.wait_for_selector(".board")
    check("board: backend card in Done, qa card back in Backlog", pg.locator('[data-col=done] .task').count() == 1 and pg.locator('[data-col=backlog] .task').count() == 1,
          f"done={pg.locator('[data-col=done] .task').count()} backlog={pg.locator('[data-col=backlog] .task').count()} review={pg.locator('[data-col=review] .task').count()}")
    pg.goto(BASE + "/#/activity"); pg.wait_for_selector(".feed")
    check("activity feed shows merge as complete", pg.locator(".ev:has-text('merged after human approval')").count() >= 1)
    check("no console/page errors", not errs, "; ".join(errs)[:200])

    # cleanup: reject the remaining frontend PR so the repo is left tidy (still human-path)
    pg.goto(BASE + "/#/overview")
    pg.click(f'button[aria-label="Reject frontend PR #{appr["frontend"]["pr"]["number"]}"]')
    pg.wait_for_selector(f'li.appr:has-text("PR #{appr["frontend"]["pr"]["number"]}") >> text=Rejected', timeout=60000)
    b.close()

server.should_exit = True
bad = [r for r in R if not r[1]]
print(f"\n{len(R)-len(bad)}/{len(R)} e2e checks passed")
sys.exit(1 if bad else 0)
