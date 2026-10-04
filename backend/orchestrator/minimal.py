"""Stage-friendly 'minimal' scope: ONE tiny file per agent so a slow/free model finishes inside the wall budget.
Enabled with KODE_MINIMAL=1 (or run_demo.py --minimal). Ownership stays valid because it only shrinks to a subset."""
from typing import List

from models import Task

ADDENDUM = (" MINIMAL SCOPE: each of the 3 agents owns exactly ONE file (backend api/routes/billing.py, frontend"
            " web/lib/api/billingClient.ts, qa tests/contract/test_billing_contract.py).")


def shrink_tasks(tasks: List[Task]) -> None:
    for t in tasks:
        t.files_owned = t.files_owned[:1]
        t.description += " Write ONLY the single owned file, under 40 lines, no commentary."
        if t.role.value == "backend":
            t.description += (" Keep it trivial: thin FastAPI route stubs matching the contract paths, returning"
                              " Pydantic model instances; no database, no stripe package, no auth.")
