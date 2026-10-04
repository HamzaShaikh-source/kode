"""Rule 1: Pydantic models validate against the frozen OpenAPI (field names, required, enums)."""
from pathlib import Path

import yaml
from openapi_spec_validator import validate

import billing_models

CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "demo_openapi.yaml"
SPEC = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def test_openapi_document_is_valid():
    validate(SPEC)


def test_required_demo_endpoints_present():
    paths = SPEC["paths"]
    assert "post" in paths["/billing/tiers"]
    assert "get" in paths["/billing/usage"]
    assert "post" in paths["/stripe/webhook"]


def test_every_schema_has_a_matching_pydantic_model():
    schemas = SPEC["components"]["schemas"]
    for name, schema in schemas.items():
        model = getattr(billing_models, name)
        js = model.model_json_schema()
        assert set(js["properties"]) == set(schema["properties"]), name
        assert set(js.get("required", [])) == set(schema.get("required", [])), name


def test_enum_values_match():
    spec_enum = SPEC["components"]["schemas"]["UsageRecord"]["properties"]["metric"]["enum"]
    assert [m.value for m in billing_models.UsageMetric] == spec_enum


def test_sample_payload_round_trips():
    t = billing_models.BillingTier(
        id="t1", name="Enterprise", stripe_price_id="price_1", monthly_price_cents=99900,
        rate_limit_per_minute=600, included_requests=1_000_000, overage_cents_per_1k=40,
        created_at="2026-10-03T00:00:00Z")
    assert billing_models.BillingTier.model_validate_json(t.model_dump_json()) == t
