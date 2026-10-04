/**
 * FROZEN TYPES for contracts/demo_openapi.yaml — generated 1:1 from the OpenAPI components.
 * Frontend code MUST import from here (alias: "@contracts/demo_types"); never invent types.
 */

export interface CreateBillingTierRequest {
  name: string;
  monthly_price_cents: number;
  rate_limit_per_minute: number;
  included_requests: number;
  overage_cents_per_1k: number;
}

export interface BillingTier {
  id: string;
  name: string;
  stripe_price_id: string;
  monthly_price_cents: number;
  rate_limit_per_minute: number;
  included_requests: number;
  overage_cents_per_1k: number;
  /** ISO-8601 date-time */
  created_at: string;
}

export type UsageMetric = "api_requests" | "seats" | "storage_gb";

export interface UsageRecord {
  id: string;
  customer_id: string;
  tier_id: string;
  metric: UsageMetric;
  quantity: number;
  /** ISO-8601 date-time */
  period_start: string;
  /** ISO-8601 date-time */
  period_end: string;
  /** ISO-8601 date-time */
  recorded_at: string;
}

export interface UsageReport {
  customer_id: string;
  tier: BillingTier;
  records: UsageRecord[];
  total_quantity: number;
  limit_exceeded: boolean;
}

export interface StripeWebhookEvent {
  id: string;
  type: string;
  created: number;
  data: Record<string, unknown>;
}

export interface WebhookAck {
  received: boolean;
}

export interface ErrorResponse {
  error: string;
  detail?: string;
}

export type GetBillingUsagePeriod = "current" | "previous";
