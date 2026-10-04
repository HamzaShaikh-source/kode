"""A scripted stand-in for an AI coding agent: lets the whole K.O.D.E. project flow be rehearsed (and tested) offline.

It waits for a line of input, reads `.kode/TASK.md` in its working folder and follows the JSON script embedded in it:

    KODE-DEMO-SCRIPT: {"steps": [{"files": {"api/a.py": "print(1)"}, "message": "feat: add a"}, ...], "push_after": [1]}

Each step writes files, commits (authored by the git identity of this laptop), and pushes after the listed steps. It then prints
`KODE_DONE <n>`. It does not use a model; it exists so the orchestration (branching, scope guard, PRs, merge order, verification) is exercised.
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path


def git(*a):
    return subprocess.run(["git", *a], capture_output=True, text=True)


def main():
    print("Scripted demo agent ready. Waiting for instructions...", flush=True)
    sys.stdin.readline()
    task = Path(".kode/TASK.md")
    text = task.read_text(encoding="utf-8") if task.exists() else ""
    m = re.search(r"KODE-DEMO-SCRIPT:\s*(\{.*\})", text)
    if not m:
        print("No script found in .kode/TASK.md; nothing to do.", flush=True)
        time.sleep(3600)
        return
    script = json.loads(m.group(1))
    branch = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    n = 0
    for i, step in enumerate(script.get("steps", []), 1):
        for rel, content in (step.get("files") or {}).items():
            p = Path(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        git("add", "-A")
        r = git("commit", "-m", step.get("message", f"work {i}"))
        if r.returncode == 0:
            n += 1
        print(f"step {i}: {step.get('message', '')}", flush=True)
        if i in script.get("push_after", [1]):
            git("push", "-u", "origin", branch)
        time.sleep(float(script.get("delay", 0.4)))
    if script.get("push_after") is None or len(script.get("steps", [])) not in script.get("push_after", []):
        git("push", "-u", "origin", branch)
    print(f"KODE_DONE {n}", flush=True)
    time.sleep(3600)


if __name__ == "__main__":
    main()
