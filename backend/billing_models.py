"""Pydantic mirrors of contracts/demo_openapi.yaml components.

tests/test_contract_models.py proves these stay field-for-field identical to the OpenAPI
document, so the contract (not this file) is the source of truth.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class UsageMetric(str, Enum):
    api_requests = "api_requests"
    seats = "seats"
    storage_gb = "storage_gb"


class CreateBillingTierRequest(BaseModel):
    name: str
    monthly_price_cents: int
    rate_limit_per_minute: int
    included_requests: int
    overage_cents_per_1k: int


class BillingTier(BaseModel):
    id: str
    name: str
    stripe_price_id: str
    monthly_price_cents: int
    rate_limit_per_minute: int
    included_requests: int
    overage_cents_per_1k: int
    created_at: datetime


class UsageRecord(BaseModel):
    id: str
    customer_id: str
    tier_id: str
    metric: UsageMetric
    quantity: int
    period_start: datetime
    period_end: datetime
    recorded_at: datetime


class UsageReport(BaseModel):
    customer_id: str
    tier: BillingTier
    records: List[UsageRecord]
    total_quantity: int
    limit_exceeded: bool


class StripeWebhookEvent(BaseModel):
    id: str
    type: str
    created: int
    data: Dict[str, Any]


class WebhookAck(BaseModel):
    received: bool


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None
