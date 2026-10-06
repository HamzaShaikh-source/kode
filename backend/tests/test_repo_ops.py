"""Repository creation, automatic collaborator access and the final hygiene audit (node ops, with `gh` replaced by a recorder)."""
import asyncio
import subprocess

import pytest

from hub.project import ProjectEngine
from node.ops import Ops

run = asyncio.run


class FakeGh(Ops):
    def __init__(self, root, replies):
        super().__init__([str(root)])
        self.calls, self.replies = [], replies

    async def sh(self, argv, cwd=None, timeout=120, shell=False):
        self.calls.append(argv)
        if argv[0] != "gh":
            return await super().sh(argv, cwd, timeout, shell)
        key = " ".join(argv[1:3])
        for k, v in self.replies.items():
            if key.startswith(k):
                return v
        return {"code": 0, "out": ""}


def test_repo_create_visibility_and_name_validation(tmp_path):
    o = FakeGh(tmp_path, {"api user": {"code": 0, "out": "naman\n"}})
    r = run(o.run("repo_create", {"forge": "github", "repo_name": "cool-app", "visibility": "public", "base": "main"}))
    assert r["repo"] == "naman/cool-app"
    create = next(c for c in o.calls if c[:3] == ["gh", "repo", "create"])
    assert "--public" in create and "--private" not in create
    o.calls.clear()
    run(o.run("repo_create", {"forge": "github", "repo_name": "cool-app", "base": "main"}))
    assert "--private" in next(c for c in o.calls if c[:3] == ["gh", "repo", "create"])          # private unless asked
    n = len(o.calls)
    bad = run(o.run("repo_create", {"forge": "github", "repo_name": "bad name; rm -rf", "base": "main"}))
    assert bad["ok"] is False and "repository name" in bad["err"] and len(o.calls) == n          # rejected before any command runs


def test_collaborator_invite_accept_and_access_check(tmp_path):
    owner = FakeGh(tmp_path, {})
    assert run(owner.run("collab_add", {"repo": "naman/app", "user": "friend-1"}))["invited"] == "friend-1"
    put = owner.calls[-1]
    assert put[:5] == ["gh", "api", "-X", "PUT", "repos/naman/app/collaborators/friend-1"] and "permission=push" in put
    assert run(owner.run("collab_add", {"repo": "naman/app", "user": "x; rm -rf /"}))["ok"] is False
    mate = FakeGh(tmp_path, {"api user/repository_invitations": {"code": 0, "out": "77\tnaman/app\n88\tother/thing\n"}})
    assert run(mate.run("collab_accept", {"repo": "naman/app"}))["accepted"] == 1
    patches = [c for c in mate.calls if "PATCH" in c]
    assert len(patches) == 1 and patches[0][4] == "user/repository_invitations/77"                  # only the matching invitation
    yes = FakeGh(tmp_path, {"api repos/naman/app": {"code": 0, "out": "true\n"}})
    no = FakeGh(tmp_path, {"api repos/naman/app": {"code": 1, "out": "Not Found"}})
    assert run(yes.run("repo_access", {"repo": "naman/app"}))["push"] is True
    assert run(no.run("repo_access", {"repo": "naman/app"}))["push"] is False


def _git(d, *a):
    subprocess.run(["git", *a], cwd=d, check=True, capture_output=True)


def test_repo_audit_flags_what_a_reviewer_would(tmp_path):
    r = tmp_path / "r"
    (r / "src").mkdir(parents=True)
    (r / "__pycache__").mkdir()
    (r / "src" / "a.py").write_text("# TODO: finish this\nKEY = 'sk-" + "a" * 40 + "'\n", encoding="utf-8")
    (r / "__pycache__" / "a.pyc").write_bytes(b"x")
    (r / "README.md").write_text("# x\n", encoding="utf-8")
    _git(r, "init", "-q")
    _git(r, "add", "-f", "-A")
    a = run(Ops([str(tmp_path)]).run("repo_audit", {"dir": str(r)}))
    assert a["stray"] and a["todos"] and a["secrets"] and not a["has_tests"] and a["readme_len"] < 400
    titles = [i["title"] for i in ProjectEngine.hygiene_issues(None, a)]
    assert any("README" in t for t in titles) and any("secrets" in t.lower() for t in titles) and any("TODO" in t for t in titles) and any("files that do not belong" in t for t in titles)


def test_a_clean_repo_has_no_hygiene_issues(tmp_path):
    r = tmp_path / "r"
    (r / "tests").mkdir(parents=True)
    (r / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (r / "tests" / "test_app.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    (r / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    (r / "README.md").write_text("# App\n\nWhat it is. " + "It prints a greeting for visitors. " * 14 + "\n\n## Run\n\n`python app.py`\n\n## Test\n\n`pytest`\n", encoding="utf-8")
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    a = run(Ops([str(tmp_path)]).run("repo_audit", {"dir": str(r)}))
    assert ProjectEngine.hygiene_issues(None, a) == [] and a["has_tests"] and a["has_gitignore"]


class Stub(ProjectEngine):
    """Engine with the node round-trips replaced, to test the access automation's decisions."""
    def __init__(self, access):
        self.access, self.sent, self.accepted = access, [], set()

    def log(self, *a, **k):
        pass

    async def op(self, nid, name, timeout=120, **a):
        self.sent.append((nid, name))
        if name == "repo_access":
            return {"ok": True, "push": self.access.get(nid, False) or nid in self.accepted}
        if name == "collab_accept":
            self.accepted.add(nid)
            return {"ok": True, "accepted": 1}
        return {"ok": True}

    async def must(self, p, nid, name, timeout=120, **a):
        return await self.op(nid, name, timeout, **a)


def _proj():
    return {"repo": "naman/app", "coordinator": "n1", "members": [
        {"node_id": "n1", "name": "Naman", "gh_user": "naman"}, {"node_id": "n2", "name": "Asha", "gh_user": "asha"}, {"node_id": "n3", "name": "Ben", "gh_user": "naman"}]}


def test_access_automation_invites_only_who_needs_it():
    e = Stub({"n1": True, "n3": True})
    run(e.grant_access(_proj()))
    names = [(n, k) for n, k in e.sent if k in ("collab_add", "collab_accept")]
    assert names == [("n1", "collab_add"), ("n2", "collab_accept")]       # Asha invited by the owner's laptop and accepts on her own; Ben shares the owner's account
    assert ("n2", "repo_access") in e.sent


def test_access_automation_names_who_still_cannot_push():
    class Never(Stub):
        async def op(self, nid, name, timeout=120, **a):
            if name == "collab_accept":
                return {"ok": True, "accepted": 0}
            return await super().op(nid, name, timeout, **a) if name != "repo_access" else {"ok": True, "push": nid != "n2"}

    async def go():
        import hub.project as hp
        real = hp.asyncio.sleep
        hp.asyncio.sleep = lambda s: real(0)
        try:
            await Never({}).grant_access(_proj())
        finally:
            hp.asyncio.sleep = real
    with pytest.raises(RuntimeError, match=r"Asha \(@asha\)"):
        run(go())


def test_repo_description_never_contains_control_characters(tmp_path):
    o = FakeGh(tmp_path, {"api user": {"code": 0, "out": "naman\n"}})
    r = run(o.run("repo_create", {"forge": "github", "repo_name": "flappy", "base": "main", "description": "Flappy bird\n\nwith\ttabs and a \x07 bell"}))
    assert r["ok"]
    create = next(c for c in o.calls if c[:3] == ["gh", "repo", "create"])
    d = create[create.index("--description") + 1]
    assert d == "Flappy bird with tabs and a bell" and not any(ord(ch) < 32 for ch in d)
