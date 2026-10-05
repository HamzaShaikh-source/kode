"""Joining from another laptop: invite link -> join API -> installers -> self-contained agent bundle -> paired machine.

The end-to-end test unpacks the bundle into an empty folder OUTSIDE the backend and runs the agent from there, which is what a
teammate's laptop does: if the bundle missed a module, or the agent imported anything from the Brain's code, it would fail here.
"""
import asyncio
import io
import json
import os
import subprocess
import sys
import zipfile

import httpx
import pytest

from hub.join import BUNDLE_FILES, install_ps1, install_sh
from tests.hubkit import Console, HubServer, until

run = asyncio.run
win = pytest.mark.skipif(sys.platform != "win32", reason="the node spawns Windows shells in this environment")
FORBIDDEN = (".env", "hub.db", ".hub_key", "hub.secret", "demo_workspaces", "scratch_run", "kode_state.db", ".demo_nodes")


async def new_invite(hub, member="Nishitha", access="full", ttl="1h"):
    async with Console(hub.base, "lead", hub.lead_key) as lead:
        r = await lead.call(t="create_invite", member=member, access=access, ttl=ttl)
        assert r["ok"]
        return r["code"]


def test_bundle_is_an_allow_list_with_no_secrets(tmp_path):
    with HubServer(tmp_path) as hub:
        z = zipfile.ZipFile(io.BytesIO(httpx.get(hub.base + "/join/agent.zip").content))
        names = z.namelist()
        assert "node/agent.py" in names and "requirements-node.txt" in names and "bridge/manifests/claude.yaml" in names
        assert not any(f in n for n in names for f in FORBIDDEN), names
        assert all(n in BUNDLE_FILES or n.startswith("bridge/manifests/") or n in ("requirements-node.txt", "README.txt") for n in names)
        blob = b"".join(z.read(n) for n in names)
        assert hub.lead_key.encode() not in blob


def test_join_api_installers_and_lockout(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            code = await new_invite(hub, "Nishitha", "full", "24h")
            j = httpx.get(f"{hub.base}/join/api/{code}").json()
            assert j["status"] == "valid" and j["member"] == "Nishitha" and j["access"] == "full" and 80000 < j["expires_in"] <= 86400
            assert j["brain"] == hub.base and "key" not in json.dumps(j).lower()
            ps1 = httpx.get(f"{hub.base}/join/{code}/install.ps1").text
            sh = httpx.get(f"{hub.base}/join/{code}/install.sh").text
            for script in (ps1, sh):
                assert hub.base in script and code in script and hub.lead_key not in script
            dl = httpx.get(f"{hub.base}/join/{code}/install.ps1?dl=1")                           # the Windows "Download installer" button
            assert dl.headers["content-disposition"].startswith("attachment") and dl.content[:3] == bytes([239, 187, 191]) and b"\r\n" in dl.content
            assert httpx.get(f"{hub.base}/join/{code}").status_code == 200                  # the page itself
            assert "Content-Security-Policy" in httpx.get(f"{hub.base}/join/{code}").headers
            # unknown / malformed codes are authentication failures, and repeated ones lock the address out
            assert httpx.get(f"{hub.base}/join/api/ABCD-ABCD-ABCD").status_code == 404
            for _ in range(8):
                httpx.get(f"{hub.base}/join/api/nope")
            assert httpx.get(f"{hub.base}/join/api/{code}").status_code == 429
    run(go())


def test_installer_quotes_hostile_names():
    s = install_sh("http://brain:8000", "AAAA-BBBB-CCCC", "x'; rm -rf ~; echo '")
    assert "MEMBER='x'\\''; rm -rf ~; echo '\\'''" in s                              # stays one quoted word
    p = install_ps1("http://brain:8000", "AAAA-BBBB-CCCC", "x'; Remove-Item C:\\ -Recurse; '")
    assert "$Member = 'x''; Remove-Item C:\\ -Recurse; '''" in p


def test_lead_key_never_served_through_a_tunnel_or_proxy(tmp_path):
    with HubServer(tmp_path) as hub:
        direct = httpx.get(hub.base + "/console").text
        assert hub.lead_key in direct                                                  # the Brain's own PC, direct
        for hdr in ({"X-Forwarded-For": "203.0.113.9"}, {"CF-Connecting-IP": "203.0.113.9"}, {"Host": "evil.trycloudflare.com"}):
            assert hub.lead_key not in httpx.get(hub.base + "/console", headers=hdr).text, hdr


@win
def test_a_new_laptop_joins_with_only_the_bundle(tmp_path):
    async def go():
        with HubServer(tmp_path) as hub:
            code = await new_invite(hub, "Jyoti", "full")
            laptop = tmp_path / "laptop"
            (laptop / "app").mkdir(parents=True)
            zipfile.ZipFile(io.BytesIO(httpx.get(hub.base + "/join/agent.zip").content)).extractall(laptop / "app")
            env = dict(os.environ)
            env.update({"PYTHONPATH": os.environ.get("PYTHONPATH", ""), "PYTHONIOENCODING": "utf-8"})       # third-party deps only: no Brain code
            p = subprocess.Popen([sys.executable, str(laptop / "app" / "node" / "agent.py"), "--hub", hub.base, "--pair", code, "--name", "Jyoti's laptop",
                                  "--workspace", str(laptop / "ws"), "--config", str(laptop / "node.json")],
                                 cwd=str(laptop), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            hub.nodes.append(p)
            async with Console(hub.base, "lead", hub.lead_key) as lead:
                await until(lambda: lead.nodes().get("Jyoti's laptop", {}).get("online"), 40)
                n = lead.nodes()["Jyoti's laptop"]
                assert n["member_name"] == "Jyoti" and n["access"] == "full"
                await until(lambda: lead.nodes()["Jyoti's laptop"].get("info", {}).get("checked"), 30)   # readiness reported for the Team page
                assert "git" in lead.nodes()["Jyoti's laptop"]["info"]
            assert httpx.get(f"{hub.base}/join/api/{code}").json()["status"] == "used"
            assert httpx.get(f"{hub.base}/join/{code}/install.ps1").status_code == 410       # a used invite cannot be installed again
    run(go())
