"""Event schema for the Phase 3 clickstream layer.

An ``OrderEvent`` is one e-commerce order as it happens. It is deliberately
self-contained — it carries the customer attributes (age, region) alongside the
order so the consumer can populate the existing ``customers`` and ``orders``
data-mart tables without a separate customer feed. The fields mirror
``app.core.schema_def`` so the streamed data is queryable by the unchanged SQL
agent.

Per Architecture Rule 2 these events are operational records, not LLM input:
they flow producer -> Kafka -> consumer -> Postgres. The LLM only ever sees the
schema and aggregate results, never individual events.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class OrderEvent(BaseModel):
    order_id: int = Field(..., description="Unique order identifier (orders PK).")
    customer_id: int = Field(..., description="Customer identifier (customers PK).")
    order_date: str = Field(..., description="ISO date the order was placed.")

    # Customer attributes (confounders for the causal model).
    age: int = Field(..., ge=0)
    region: str = Field(..., description="One of: NA, EU, APAC, LATAM.")
    loyalty_tier: str | None = Field(
        default=None, description="One of: bronze, silver, gold. Confounds saw_banner."
    )

    # Order attributes (treatments + outcome). The extra treatments default to 0
    # so older/poison-pill payloads still validate; the live stream sets them.
    received_discount: int = Field(..., ge=0, le=1, description="Treatment flag 0/1.")
    discount_pct: float = Field(0.0, ge=0)
    ui_variant_b: int = Field(0, ge=0, le=1, description="Treatment: checkout UI variant B.")
    saw_banner: int = Field(0, ge=0, le=1, description="Treatment (placebo): free-shipping banner.")
    total_amount: float = Field(..., ge=0, description="Outcome: order value in USD.")
    num_items: int = Field(1, ge=1)
