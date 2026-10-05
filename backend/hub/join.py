"""Joining a team from ANY laptop: invite link -> join page -> one-line installer -> self-contained agent bundle -> paired node.

  GET /join/{code}                 the page a teammate opens (static HTML; reads the code from the URL)
  GET /join/api/{code}             invite status for that page: valid / used / expired, member name, access policy. No secrets.
  GET /join/{code}/install.ps1     Windows installer (PowerShell)       } download the bundle, make a venv, pair with the code,
  GET /join/{code}/install.sh      macOS / Linux installer (sh)         } and leave a start script for later runs
  GET /join/agent.zip              the node agent bundle: an explicit allow-list of source files, never the backend folder

Security notes
  * Nothing here needs the lead key and nothing here returns one: the invite code is the only capability, single use and short lived.
  * Unknown codes count as authentication failures (shared lockout with pairing / viewer keys), so codes cannot be brute forced.
  * The bundle is built from an allow-list (no .env, databases, keys, workspaces, scratch files), and the installers only embed the
    Brain URL the teammate already used plus the invite code.
  * "Is this request local?" never trusts a loopback client address alone: a tunnel or reverse proxy on the Brain's own PC also
    connects from 127.0.0.1, so forwarded headers or a non-local Host header mean "remote".
"""
from __future__ import annotations

import io
import re
import time
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response

BACKEND = Path(__file__).resolve().parents[1]
CODE_RE = re.compile(r"^[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}$")
BUNDLE_FILES = [
    "node/__init__.py", "node/agent.py", "node/ops.py", "node/collectors.py",
    "bridge/__init__.py", "bridge/manifests.py", "bridge/pty_session.py", "bridge/demo_agent.py",
    "hub/__init__.py", "hub/protocol.py", "hub/apiprofiles.py",
]
BUNDLE_GLOBS = ["bridge/manifests/*.yaml"]
REQUIREMENTS = """websockets>=12
PyYAML>=6.0
httpx>=0.27
pywinpty>=2.0; sys_platform == "win32"
ptyprocess>=0.7; sys_platform != "win32"
"""
README = """K.O.D.E. node agent
===================
This folder was installed by your team's K.O.D.E. Brain. It connects OUTWARD to the Brain and hosts your coding agents'
terminals so the team lead can see and coordinate them. Your own agents (Claude Code, Codex, Antigravity, ...) and your own
git / GitHub CLI login do the actual work; nothing here holds your GitHub token.

Start it again later:   Windows: start.ps1     macOS / Linux: ./start.sh
Stop it:                Ctrl+C in its window.
Remove it:              delete this folder. The lead can also remove your machine from the Team page.
You can always pull the kill switch from your member console (its link is printed when the agent connects).
"""

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
FORWARD_HEADERS = ("x-forwarded-for", "x-forwarded-host", "forwarded", "cf-connecting-ip", "x-real-ip", "true-client-ip")


def is_local_request(request: Request) -> bool:
    """True only for a browser on THIS PC talking to the Brain directly (not through a tunnel or proxy)."""
    if not request.client or request.client.host not in LOCAL_HOSTS:
        return False
    if any(request.headers.get(h) for h in FORWARD_HEADERS):
        return False
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    return host in LOCAL_HOSTS


def base_url(request: Request) -> str:
    """The Brain URL exactly as this visitor reached it (works for LAN IPs and HTTPS tunnels alike)."""
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    host = host.split(",")[0].strip()
    if not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]{1,255}", host):
        raise HTTPException(400, "bad host header")
    return f"{proto}://{host}"


def build_bundle() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        files = list(BUNDLE_FILES)
        for g in BUNDLE_GLOBS:
            files += sorted(str(p.relative_to(BACKEND)).replace("\\", "/") for p in BACKEND.glob(g))
        for rel in files:
            src = BACKEND / rel
            if src.is_file():
                z.write(src, rel)
        z.writestr("requirements-node.txt", REQUIREMENTS)
        z.writestr("README.txt", README)
    return buf.getvalue()


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def install_ps1(base: str, code: str, member: str) -> str:
    return f"""# K.O.D.E. node installer for Windows. Generated for invite {code}; safe to read before running.
$ErrorActionPreference = 'Stop'
$Brain = {_ps_quote(base)}
$Code = {_ps_quote(code)}
$Member = {_ps_quote(member or 'Teammate')}
$Dir = Join-Path $env:USERPROFILE 'kode-node'
$Ws = Join-Path $env:USERPROFILE 'kode-workspace'
New-Item -ItemType Directory -Force -Path $Dir, $Ws | Out-Null
Write-Host "K.O.D.E.: joining the team at $Brain as $Member" -ForegroundColor Cyan
$py = $null
foreach ($c in @('python', 'py', 'python3')) {{ if (Get-Command $c -ErrorAction SilentlyContinue) {{ $py = $c; break }} }}
if (-not $py) {{ Write-Host 'Python 3.10+ is required: install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH") and run this again.' -ForegroundColor Red; return }}
Write-Host '1/4 downloading the agent bundle from your Brain'
Invoke-WebRequest "$Brain/join/agent.zip" -OutFile (Join-Path $Dir 'agent.zip') -UseBasicParsing
if (Test-Path (Join-Path $Dir 'app')) {{ Remove-Item -Recurse -Force (Join-Path $Dir 'app') }}
Expand-Archive (Join-Path $Dir 'agent.zip') -DestinationPath (Join-Path $Dir 'app') -Force
Write-Host '2/4 creating a private Python environment'
if (-not (Test-Path (Join-Path $Dir 'venv'))) {{ & $py -m venv (Join-Path $Dir 'venv') }}
$vpy = Join-Path $Dir 'venv\\Scripts\\python.exe'
& $vpy -m pip install --disable-pip-version-check -q -r (Join-Path $Dir 'app\\requirements-node.txt')
Write-Host '3/4 checking your tools'
if (Get-Command git -ErrorAction SilentlyContinue) {{ Write-Host '  git: ok' }} else {{ Write-Host '  git: MISSING (install from https://git-scm.com)' -ForegroundColor Yellow }}
if (Get-Command gh -ErrorAction SilentlyContinue) {{ gh auth status 2>$null | Out-Null; if ($LASTEXITCODE -eq 0) {{ Write-Host '  GitHub CLI: logged in' }} else {{ Write-Host '  GitHub CLI: run  gh auth login' -ForegroundColor Yellow }} }} else {{ Write-Host '  GitHub CLI: MISSING (https://cli.github.com), then gh auth login' -ForegroundColor Yellow }}
foreach ($a in @('claude', 'codex', 'agy', 'opencode', 'hermes')) {{ if (Get-Command $a -ErrorAction SilentlyContinue) {{ Write-Host "  agent found: $a" }} }}
$Name = "$Member's $env:COMPUTERNAME"
$Cfg = Join-Path $Dir 'node.json'
$Agent = Join-Path $Dir 'app\\node\\agent.py'
$Run = "& '$vpy' '$Agent' --hub '$Brain' --name '$($Name -replace "'", "''")' --workspace '$Ws' --config '$Cfg'"
# The agent restarts itself if it crashes and reconnects on its own when the network drops. Exit code 3 means only a new invite helps.
Set-Content -Encoding UTF8 (Join-Path $Dir 'start.ps1') ($Run + [Environment]::NewLine + 'while ($LASTEXITCODE -ne 3) {{ Write-Host ''K.O.D.E. agent stopped; restarting in 5s (Ctrl+C to quit)''; Start-Sleep 5; ' + $Run + ' }}')
Write-Host '4/4 connecting (keep this window open; Ctrl+C stops it, start.ps1 starts it again)' -ForegroundColor Cyan
& $vpy $Agent --hub $Brain --pair $Code --name $Name --workspace $Ws --config $Cfg
while ($LASTEXITCODE -ne 3 -and (Test-Path $Cfg)) {{ Write-Host 'K.O.D.E. agent stopped; restarting in 5s (Ctrl+C to quit)'; Start-Sleep 5; Invoke-Expression $Run }}
"""


def install_sh(base: str, code: str, member: str) -> str:
    q = lambda s: "'" + s.replace("'", "'\\''") + "'"
    return f"""#!/bin/sh
# K.O.D.E. node installer for macOS / Linux. Generated for invite {code}; safe to read before running.
set -eu
BRAIN={q(base)}
CODE={q(code)}
MEMBER={q(member or 'Teammate')}
DIR="$HOME/kode-node"
WS="$HOME/kode-workspace"
mkdir -p "$DIR" "$WS"
echo "K.O.D.E.: joining the team at $BRAIN as $MEMBER"
PY=""
for c in python3 python; do if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi; done
if [ -z "$PY" ]; then echo "Python 3.10+ is required (https://www.python.org/downloads/), then run this again." >&2; exit 1; fi
echo "1/4 downloading the agent bundle from your Brain"
curl -fsSL "$BRAIN/join/agent.zip" -o "$DIR/agent.zip"
rm -rf "$DIR/app" && "$PY" -m zipfile -e "$DIR/agent.zip" "$DIR/app"
echo "2/4 creating a private Python environment"
[ -d "$DIR/venv" ] || "$PY" -m venv "$DIR/venv"
"$DIR/venv/bin/python" -m pip install --disable-pip-version-check -q -r "$DIR/app/requirements-node.txt"
echo "3/4 checking your tools"
if command -v git >/dev/null 2>&1; then echo "  git: ok"; else echo "  git: MISSING (https://git-scm.com)"; fi
if command -v gh >/dev/null 2>&1; then if gh auth status >/dev/null 2>&1; then echo "  GitHub CLI: logged in"; else echo "  GitHub CLI: run  gh auth login"; fi; else echo "  GitHub CLI: MISSING (https://cli.github.com), then gh auth login"; fi
for a in claude codex agy opencode hermes; do if command -v "$a" >/dev/null 2>&1; then echo "  agent found: $a"; fi; done
NAME="$MEMBER's $(hostname)"
CFG="$DIR/node.json"
AGENT="$DIR/app/node/agent.py"
# The agent restarts itself if it crashes and reconnects on its own when the network drops. Exit code 3 means only a new invite helps.
cat > "$DIR/start.sh" <<STARTEOF
#!/bin/sh
while true; do
  "$DIR/venv/bin/python" "$AGENT" --hub "$BRAIN" --name "$NAME" --workspace "$WS" --config "$CFG"
  rc=\\$?
  if [ "\\$rc" = 3 ]; then exit 3; fi
  echo "K.O.D.E. agent stopped; restarting in 5s (Ctrl+C to quit)"; sleep 5
done
STARTEOF
chmod +x "$DIR/start.sh"
echo "4/4 connecting (keep this window open; Ctrl+C stops it, $DIR/start.sh starts it again)"
"$DIR/venv/bin/python" "$AGENT" --hub "$BRAIN" --pair "$CODE" --name "$NAME" --workspace "$WS" --config "$CFG" || [ $? -ne 3 ] || exit 3
exec "$DIR/start.sh"
"""


def make_router(get_hub, console_dir: Path) -> APIRouter:
    r = APIRouter()
    cache = {"zip": None, "at": 0.0}

    def guard(request: Request, code: str):
        hub = get_hub()
        if hub is None:
            raise HTTPException(404, "the Brain is not enabled")
        ip = request.client.host if request.client else "?"
        if hub.ip_blocked(ip):
            raise HTTPException(429, "too many attempts; wait a few minutes")
        if not CODE_RE.match(code or ""):
            hub.ip_fail(ip, "malformed invite code")
            raise HTTPException(404, "unknown invite")
        info = hub.store.invite_status(code)
        if info is None:
            hub.ip_fail(ip, "unknown invite code")
            raise HTTPException(404, "unknown invite")
        return hub, info

    @r.get("/join/agent.zip", include_in_schema=False)
    async def agent_zip():
        if get_hub() is None:
            raise HTTPException(404)
        if cache["zip"] is None or time.time() - cache["at"] > 60:
            cache["zip"], cache["at"] = build_bundle(), time.time()
        return Response(cache["zip"], media_type="application/zip", headers={"Content-Disposition": "attachment; filename=kode-node.zip", "Cache-Control": "no-store"})

    @r.get("/join/api/{code}", include_in_schema=False)
    async def join_info(code: str, request: Request):
        _, info = guard(request, code)
        return JSONResponse({**info, "brain": base_url(request)}, headers={"Cache-Control": "no-store"})

    @r.get("/join/{code}/install.ps1", include_in_schema=False)
    async def ps1(code: str, request: Request):
        _, info = guard(request, code)
        if info["status"] != "valid":
            raise HTTPException(410, f"this invite is {info['status']}; ask your lead for a new one")
        body = install_ps1(base_url(request), code.upper(), info["member"])
        if request.query_params.get("dl"):
            # A saved file: UTF-8 with BOM so Windows PowerShell reads any non-ASCII name correctly, CRLF line endings.
            return Response(("\ufeff" + body.replace("\r\n", "\n").replace("\n", "\r\n")).encode("utf-8"), media_type="application/octet-stream",
                            headers={"Content-Disposition": 'attachment; filename="kode-join.ps1"', "Cache-Control": "no-store"})
        return PlainTextResponse(body, headers={"Cache-Control": "no-store"})

    @r.get("/join/{code}/install.sh", include_in_schema=False)
    async def sh(code: str, request: Request):
        _, info = guard(request, code)
        if info["status"] != "valid":
            raise HTTPException(410, f"this invite is {info['status']}; ask your lead for a new one")
        return PlainTextResponse(install_sh(base_url(request), code.upper(), info["member"]), headers={"Cache-Control": "no-store"})

    @r.get("/join/{code}", include_in_schema=False)
    async def page(code: str):
        if get_hub() is None:
            raise HTTPException(404)
        return FileResponse(console_dir / "join.html", media_type="text/html", headers={"Cache-Control": "no-store"})

    return r
