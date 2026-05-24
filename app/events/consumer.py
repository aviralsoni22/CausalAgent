"""Threshold-triggering Kafka consumer — the heart of the Phase 3 event layer.

Reads order events, writes them into the existing ``customers``/``orders``
data-mart tables, and counts how many genuinely-new rows it has ingested. When
the count crosses a multiple of ``KAFKA_TRIGGER_THRESHOLD`` it enqueues one
``run_causal_analysis`` task on the same Celery queue the synchronous API uses.

This is "Option B": the stream feeds the data mart continuously and
autonomously triggers a causal analysis when enough new data has accumulated —
reusing the entire existing SQL -> R -> evaluate -> review pipeline unchanged.

Delivery semantics (at-least-once):
- Kafka auto-commit is OFF. The offset is committed only *after* the database
  transaction for that event has committed, so a crash mid-event redelivers it
  rather than silently dropping it.
- Inserts are idempotent (``ON CONFLICT DO NOTHING``), so a redelivered event
  produces no duplicate row.
- The ingest counter lives in Postgres (``ingest_state``) and is incremented
  only when a row was *actually* inserted (detected via ``RETURNING``), so a
  redelivered duplicate never double-counts. The counter therefore survives
  restarts — the in-memory reset bug of the previous version is gone.
- A trigger fires at-most-once per threshold bucket (``triggers_fired`` is
  advanced inside the same committed transaction). The rare window is a *missed*
  trigger if the process dies between commit and enqueue — preferred over a
  duplicate (and costly) LLM run.

The analysis is enqueued by name via a thin Celery client, so this process does
not import the orchestrator/LLM stack.
"""
from __future__ import annotations

import json
import logging
import uuid

from celery import Celery
from kafka import KafkaConsumer
from pydantic import ValidationError
from sqlalchemy import text

from app.core import config
from app.core.db import get_engine
from app.events.schemas import OrderEvent

logger = logging.getLogger(__name__)

_celery = Celery(
    "causal-consumer-client",
    broker=config.CELERY_BROKER_URL,
    backend=config.CELERY_RESULT_BACKEND,
)

# The causal question the accumulated stream answers. Pinned (treatment /
# outcome / confounders fixed) so a triggered run is deterministic and matches
# the generative model behind the streamed data.
_TRIGGER_QUERY = "Did receiving a discount increase the order total?"
_TRIGGER_SPEC = {
    "treatment": "received_discount",
    "outcome": "total_amount",
    "confounders": ["age"],
}

_INGEST_DDL = """
CREATE TABLE IF NOT EXISTS ingest_state (
    id             BOOLEAN PRIMARY KEY DEFAULT TRUE,
    ingested       BIGINT NOT NULL DEFAULT 0,
    triggers_fired BIGINT NOT NULL DEFAULT 0,
    window_lo      BIGINT NOT NULL DEFAULT 0,
    CONSTRAINT ingest_state_singleton CHECK (id)
)
"""
# Idempotent migration for an ingest_state created before window_lo existed.
_INGEST_MIGRATE = (
    "ALTER TABLE ingest_state ADD COLUMN IF NOT EXISTS window_lo BIGINT NOT NULL DEFAULT 0"
)
_INGEST_INIT = (
    "INSERT INTO ingest_state (id) VALUES (TRUE) ON CONFLICT (id) DO NOTHING"
)

_INSERT_CUSTOMER = text(
    """
    INSERT INTO customers (customer_id, signup_date, region, age, loyalty_tier)
    VALUES (:customer_id, NULL, :region, :age, NULL)
    ON CONFLICT (customer_id) DO NOTHING
    """
)
_INSERT_ORDER = text(
    """
    INSERT INTO orders (order_id, customer_id, order_date, received_discount,
                        discount_pct, total_amount, num_items)
    VALUES (:order_id, :customer_id, :order_date, :received_discount,
            :discount_pct, :total_amount, :num_items)
    ON CONFLICT (order_id) DO NOTHING
    RETURNING order_id
    """
)


def trigger_due(ingested: int, triggers_fired: int, threshold: int) -> bool:
    """True if another analysis is owed given the durable counts.

    Pure decision function (no I/O) so the threshold-boundary behaviour is
    unit-testable without Kafka or a database.
    """
    if threshold <= 0:
        return False
    return ingested // threshold > triggers_fired


def ensure_ingest_state(conn) -> None:
    conn.execute(text(_INGEST_DDL))
    conn.execute(text(_INGEST_MIGRATE))
    conn.execute(text(_INGEST_INIT))


def ingest_event(conn, event: OrderEvent, threshold: int) -> dict | None:
    """Persist one event and update the durable counter in a single transaction.

    Returns the tumbling window ``{"lo": int, "hi": int}`` to analyse if this
    event crossed a threshold boundary, else None. The window spans the orders
    ingested since the previous trigger: order_id in (lo, hi]. This assumes the
    stream's order_id increases with arrival (true for our producer); a
    production feed would key the window on an ingestion sequence/timestamp
    column instead. The caller enqueues the task *after* the transaction commits.
    """
    # Customer first so the order's foreign key is satisfied; both idempotent.
    conn.execute(
        _INSERT_CUSTOMER,
        {"customer_id": event.customer_id, "region": event.region, "age": event.age},
    )
    inserted = conn.execute(
        _INSERT_ORDER,
        {
            "order_id": event.order_id,
            "customer_id": event.customer_id,
            "order_date": event.order_date,
            "received_discount": event.received_discount,
            "discount_pct": event.discount_pct,
            "total_amount": event.total_amount,
            "num_items": event.num_items,
        },
    ).first()

    # Redelivered duplicate: row already existed, so do not count or trigger.
    if inserted is None:
        return None

    row = conn.execute(
        text(
            "UPDATE ingest_state SET ingested = ingested + 1 "
            "WHERE id RETURNING ingested, triggers_fired, window_lo"
        )
    ).first()
    ingested, triggers_fired, window_lo = int(row[0]), int(row[1]), int(row[2])
    if not trigger_due(ingested, triggers_fired, threshold):
        return None

    # Reserve the trigger and advance the watermark to this order_id, inside the
    # same transaction, so the next window starts where this one ends.
    hi = event.order_id
    conn.execute(
        text(
            "UPDATE ingest_state SET triggers_fired = triggers_fired + 1, "
            "window_lo = :hi WHERE id"
        ),
        {"hi": hi},
    )
    return {"lo": window_lo, "hi": hi}


def parse_event(value) -> OrderEvent | None:
    """Validate a raw message payload into an ``OrderEvent``.

    Returns None for a poison pill — a payload that can never be valid (wrong
    shape, missing/invalid fields). The caller skips and commits past it rather
    than retrying forever. Kept narrow on purpose: only schema validation
    happens here, so a transient database failure stays a raised exception in
    the caller and preserves at-least-once redelivery.
    """
    try:
        return OrderEvent(**value)
    except (ValidationError, TypeError) as exc:
        logger.error("Skipping poison-pill message: %s", exc)
        return None


def _trigger_analysis(window: dict) -> str:
    task_id = uuid.uuid4().hex
    _celery.send_task(
        "run_causal_analysis",
        args=[task_id, _TRIGGER_QUERY, _TRIGGER_SPEC, window],
    )
    logger.info(
        "Threshold reached -> enqueued analysis task %s for window %s",
        task_id,
        window,
    )
    return task_id


def run() -> None:
    consumer = KafkaConsumer(
        config.KAFKA_TOPIC,
        group_id=config.KAFKA_CONSUMER_GROUP,
        value_deserializer=lambda b: json.loads(b.decode("utf-8")),
        auto_offset_reset="earliest",
        enable_auto_commit=False,
        **config.kafka_client_kwargs(),
    )
    engine = get_engine()
    with engine.begin() as conn:
        ensure_ingest_state(conn)

    logger.info(
        "Consumer started on topic '%s'; trigger threshold=%d",
        config.KAFKA_TOPIC,
        config.KAFKA_TRIGGER_THRESHOLD,
    )
    for msg in consumer:
        # A malformed/invalid event is a poison pill: it can never succeed, so
        # retrying it forever would wedge the whole partition. Skip it (commit
        # the offset to advance past it) and keep going. A transient *database*
        # failure raises below, outside this guard, so the offset stays
        # uncommitted and the event is redelivered (at-least-once preserved).
        event = parse_event(msg.value)
        if event is None:
            consumer.commit()
            continue

        with engine.begin() as conn:
            window = ingest_event(conn, event, config.KAFKA_TRIGGER_THRESHOLD)
        # Commit the offset only after the DB transaction has committed.
        consumer.commit()
        if window is not None:
            _trigger_analysis(window)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    run()
