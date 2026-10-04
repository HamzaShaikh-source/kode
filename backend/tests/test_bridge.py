"""Terminal bridge: the lock-downs matter more than the happy path (it starts programs on the host)."""
import json
import sys

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import create_app
from bridge.manifests import Manifest, load_manifests, scrubbed_env
from tests.conftest import make_settings

needs_pty = pytest.mark.skipif(sys.platform != "win32" and False, reason="pty")


def _client(tmp_path, enabled=True):
    return TestClient(create_app(make_settings(tmp_path, kode_enable_bridge=enabled, kode_bridge_cwd=str(tmp_path / "wd"),
                                               kode_frontend_dir=str(__import__("pathlib").Path(__file__).resolve().parents[2]))))


def _token(c):
    import re
    return re.search(r'const TOKEN = "([^"]+)"', c.get("/spike").text).group(1)


def test_manifests_load_and_name_known_providers():
    m = load_manifests()
    assert {"shell", "claude", "codex", "opencode", "hermes", "agy"} <= set(m)
    assert all(isinstance(x.command, list) and x.command for x in m.values())


def test_bad_manifest_rejected(tmp_path):
    (tmp_path / "x.yaml").write_text("id: x\nlabel: X\ncommand: not-a-list\n")
    with pytest.raises(ValueError):
        load_manifests(tmp_path)
    (tmp_path / "x.yaml").write_text("id: ../evil\nlabel: X\ncommand: [cmd]\n")
    with pytest.raises(ValueError):
        load_manifests(tmp_path)


def test_env_scrub_removes_secrets_unless_passed_through():
    src = {"PATH": "p", "OPENAI_API_KEY": "sk-secret", "GITHUB_TOKEN": "t", "SYSTEMROOT": "C:\\Windows", "RANDOM_VAR": "x",
           "ANTHROPIC_API_KEY": "a"}
    plain = scrubbed_env(Manifest("p", "P", ["cmd"]), src)
    assert "OPENAI_API_KEY" not in plain and "GITHUB_TOKEN" not in plain and "RANDOM_VAR" not in plain
    assert plain["PATH"] == "p"
    opted = scrubbed_env(Manifest("p", "P", ["cmd"], env_passthrough=["ANTHROPIC_API_KEY"]), src)
    assert opted["ANTHROPIC_API_KEY"] == "a" and "OPENAI_API_KEY" not in opted


def test_bridge_is_off_by_default(tmp_path):
    with _client(tmp_path, enabled=False) as c:
        assert c.get("/spike").status_code == 404
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/spike/ws?provider=shell&token=x"):
                pass


def test_ws_requires_token(tmp_path):
    with _client(tmp_path) as c:
        for bad in ("", "wrong"):
            with pytest.raises(WebSocketDisconnect) as e:
                with c.websocket_connect(f"/spike/ws?provider=shell&token={bad}"):
                    pass
            assert e.value.code == 4403


def test_ws_rejects_cross_origin(tmp_path):
    with _client(tmp_path) as c:
        t = _token(c)
        with pytest.raises(WebSocketDisconnect) as e:
            with c.websocket_connect(f"/spike/ws?provider=shell&token={t}", headers={"origin": "https://evil.example"}):
                pass
        assert e.value.code == 4403


def test_unknown_provider_and_arbitrary_command_refused(tmp_path):
    with _client(tmp_path) as c:
        t = _token(c)
        for prov in ("nope", "../../Windows/System32/calc", "cmd.exe /c calc"):
            with pytest.raises(WebSocketDisconnect) as e:
                with c.websocket_connect(f"/spike/ws?provider={prov}&token={t}&cmd=calc.exe"):
                    pass
            assert e.value.code == 4404
        assert not c.app.state.bridge.sessions


def test_providers_endpoint_needs_token(tmp_path):
    with _client(tmp_path) as c:
        assert c.get("/spike/providers?token=bad").status_code == 403
        lst = c.get(f"/spike/providers?token={_token(c)}").json()
        assert {"id", "label", "tier", "available"} <= set(lst[0])


@pytest.mark.skipif(sys.platform != "win32", reason="uses cmd.exe")
def test_real_shell_session_round_trip_and_cleanup(tmp_path):
    with _client(tmp_path) as c:
        t = _token(c)
        with c.websocket_connect(f"/spike/ws?provider=shell&token={t}&cols=100&rows=30") as ws:
            hello = json.loads(ws.receive_text())
            assert hello["t"] == "hello" and hello["pid"] > 0
            ws.send_text(json.dumps({"t": "in", "d": "echo KODE_BRIDGE_OK\r"}))
            seen = ""
            for _ in range(60):
                m = json.loads(ws.receive_text())
                if m["t"] == "out":
                    seen += m["d"]
                if seen.count("KODE_BRIDGE_OK") >= 2:          # command echo + command output
                    break
            assert seen.count("KODE_BRIDGE_OK") >= 2
            assert len(c.app.state.bridge.sessions) == 1
        import time
        for _ in range(30):                                     # session killed when the socket closes
            if not c.app.state.bridge.sessions:
                break
            time.sleep(0.1)
        assert not c.app.state.bridge.sessions
