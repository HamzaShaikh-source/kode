"""Acceptance C and D - the most important tests in the repo."""
import logging

import pytest

from models import AgentRole, Task
from orchestrator.guard import GuardedWriter, ScopeViolation, check_write, validate_ownership


def T(role, owned, tid=None):
    return Task(id=tid or f"task_{role}", feature_id="f1", role=AgentRole(role), title="t",
                description="d", files_owned=owned, contract_sha256="x")


GOOD = [
    T("backend", ["api/routes/billing.py", "api/schemas/billing.py", "db/migrations/*"]),
    T("frontend", ["web/components/BillingUsage.tsx", "web/pages/settings/billing.tsx", "web/lib/api/billingClient.ts"]),
    T("qa", ["tests/e2e/billing.spec.ts", "tests/contract/test_billing_contract.py"]),
]


def test_disjoint_ownership_accepted():
    validate_ownership(GOOD)


def test_C_two_tasks_claiming_same_file_is_rejected():
    a = T("backend", ["api/routes/billing.py"], "task_a")
    b = T("backend", ["api/routes/billing.py"], "task_b")
    with pytest.raises(ScopeViolation) as e:
        validate_ownership([a, b])
    assert e.value.task_id == "task_a" and e.value.other_task_id == "task_b"
    print("C: ScopeViolation ->", e.value)


@pytest.mark.parametrize("x,y", [
    ("db/migrations/*", "db/migrations/001_init.sql"),     # glob covers concrete file
    ("db/migrations/*", "db/migrations/*.sql"),            # glob vs glob, same dir
    ("api/", "api/routes/billing.py"),                     # directory vs file
])
def test_C_overlap_variants_rejected(x, y):
    with pytest.raises(ScopeViolation):
        validate_ownership([T("backend", [x], "a"), T("backend", [y], "b")])


def test_cross_role_claim_outside_domain_rejected():
    with pytest.raises(ScopeViolation):
        validate_ownership([T("frontend", ["api/routes/billing.py"])])


def test_frozen_contract_cannot_be_owned():
    with pytest.raises(ScopeViolation):
        validate_ownership([T("backend", ["contracts/demo_openapi.yaml"])])


@pytest.mark.parametrize("bad", ["../etc/passwd", "/abs/path.py", "api/../../x.py", "C:/x.py", "api\\..\\..\\x.py", ""])
def test_path_escape_rejected(bad):
    with pytest.raises(ScopeViolation):
        check_write(GOOD[0], bad)


def test_D_out_of_scope_write_blocked_and_logged(caplog):
    backend = GOOD[0]
    w = GuardedWriter(backend)
    w.write("api/routes/billing.py", "ok")                      # in scope
    w.write("db/migrations/001_add_tiers.sql", "ok")            # glob scope
    with caplog.at_level(logging.ERROR, logger="kode.guard"):
        with pytest.raises(ScopeViolation):
            w.write("web/components/BillingUsage.tsx", "backend stealing the frontend file")
    assert "api/routes/billing.py" in w.files and "web/components/BillingUsage.tsx" not in w.files
    msgs = [r.message for r in caplog.records]
    assert any("attempted write outside scope: web/components/BillingUsage.tsx" in m for m in msgs)
    print("D: LOG LINE ->", msgs[-1])


def test_write_to_frozen_contract_blocked():
    with pytest.raises(ScopeViolation):
        GuardedWriter(GOOD[0]).write("contracts/demo_openapi.yaml", "tampered")
