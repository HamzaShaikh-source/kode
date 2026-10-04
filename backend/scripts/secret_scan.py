"""Scan the STAGED git diff for secrets. Exit 1 if anything is found. Never prints a secret value.

  python backend/scripts/secret_scan.py

Checks: (1) forbidden paths staged, (2) well-known token shapes, (3) the literal values of every secret-like
variable in backend/.env.
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
git = lambda *a: subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout

names = [n for n in git("diff", "--cached", "--name-only").splitlines() if n]
diff = git("diff", "--cached", "-U0")
problems = []

for n in names:
    if re.search(r"(^|/)\.env($|\.)(?!example)|\.db$|\.sqlite|scratch_run/|__pycache__|\.pem$|\.key$", n) and not n.endswith(".env.example"):
        problems.append(f"forbidden path staged: {n}")

SHAPES = {
    "gateway key (thk_live_)": r"thk_live_[A-Za-z0-9_\-]{12,}",
    "github oauth/pat (gh*_)": r"\bgh[opsur]_[A-Za-z0-9]{20,}",
    "github fine-grained pat": r"github_pat_[A-Za-z0-9_]{20,}",
    "anthropic key": r"sk-ant-[A-Za-z0-9_\-]{20,}",
    "openai-style key": r"\bsk-[A-Za-z0-9]{32,}",
    "aws access key": r"\bAKIA[0-9A-Z]{16}\b",
    "private key block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
}
for label, rx in SHAPES.items():
    if re.search(rx, diff):
        problems.append(f"token shape in diff: {label}")

env = ROOT / "backend" / ".env"
if env.exists():
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            v = v.strip()
            if re.search(r"KEY|TOKEN|SECRET|PASSWORD", k, re.I) and len(v) >= 8 and v in diff:
                problems.append(f"literal value of {k} found in staged diff")

hk = ROOT / "backend" / ".hub_key"                           # the Brain's lead key is a secret too
if hk.exists() and len(hk.read_text().strip()) >= 8 and hk.read_text().strip() in diff:
    problems.append("literal lead key (.hub_key) found in staged diff")

print(f"staged files: {len(names)}")
if problems:
    print("SECRET SCAN FAILED:")
    for p in problems:
        print("  -", p)
    sys.exit(1)
print("secret scan: CLEAN (no forbidden paths, no token shapes, no literal .env secret values)")
