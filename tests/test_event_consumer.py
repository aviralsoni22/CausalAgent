"""Tests for the Phase 3 event consumer.

Two layers:
- ``trigger_due`` is a pure function; its threshold-boundary behaviour is tested
  with no infrastructure.
- ``ingest_event`` is exercised against the real Postgres data mart to prove
  idempotent counting: a redelivered (duplicate) event must not create a second
  row nor advance the durable counter. Uses a high, isolated ID range and
  restores ``ingest_state`` so it does not perturb other tests.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.db import get_engine
from app.events.consumer import (
    ensure_ingest_state,
    ingest_event,
    parse_event,
    trigger_due,
)
from app.events.schemas import OrderEvent

# Far above seed data (1..3000) and the demo stream (100000+), so this test
# never collides with rows created elsewhere.
_BASE_ID = 900_000
_NO_TRIGGER = 10_000_000  # threshold so high no analysis is enqueued mid-test


def test_trigger_due_boundaries():
    assert trigger_due(499, 0, 500) is False
    assert trigger_due(500, 0, 500) is True
    assert trigger_due(500, 1, 500) is False  # bucket already fired
    assert trigger_due(1000, 1, 500) is True  # next bucket owed
    assert trigger_due(0, 0, 500) is False
    assert trigger_due(10, 0, 0) is False  # guard against zero threshold


def test_parse_event_accepts_valid_and_rejects_poison():
    # A well-formed payload validates into an OrderEvent.
    good = {
        "order_id": 1,
        "customer_id": 1,
        "order_date": "2025-01-01",
        "age": 30,
        "region": "NA",
        "received_discount": 1,
        "total_amount": 100.0,
    }
    assert parse_event(good).order_id == 1

    # Poison pills return None (so run() skips + commits instead of crash-looping)
    # rather than raising. Missing required field, out-of-range value, and a
    # non-dict payload all count.
    assert parse_event({"order_id": 1}) is None  # missing required fields
    assert parse_event({**good, "received_discount": 5}) is None  # le=1 violated
    assert parse_event("not-a-dict") is None  # TypeError on **value
    assert parse_event(None) is None


def _event(n: int) -> OrderEvent:
    return OrderEvent(
        order_id=_BASE_ID + n,
        customer_id=_BASE_ID + n,
        order_date="2025-01-01",
        age=30 + n,
        region="NA",
        received_discount=n % 2,
        discount_pct=0.0,
        total_amount=100.0 + n,
        num_items=1,
    )


@pytest.fixture
def clean_ingest_range():
    engine = get_engine()
    with engine.begin() as conn:
        ensure_ingest_state(conn)
        row = conn.execute(
            text("SELECT ingested, triggers_fired, window_lo FROM ingest_state")
        ).first()
        before = {"ingested": int(row[0]), "triggers_fired": int(row[1]), "window_lo": int(row[2])}
    yield engine, before
    # Restore: remove the rows we created and reset all counters to prior values.
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM orders WHERE order_id >= :b AND order_id < :e"),
            {"b": _BASE_ID, "e": _BASE_ID + 1000},
        )
        conn.execute(
            text("DELETE FROM customers WHERE customer_id >= :b AND customer_id < :e"),
            {"b": _BASE_ID, "e": _BASE_ID + 1000},
        )
        conn.execute(
            text(
                "UPDATE ingest_state SET ingested = :ingested, "
                "triggers_fired = :triggers_fired, window_lo = :window_lo WHERE id"
            ),
            before,
        )


def test_ingest_counts_new_rows_and_ignores_duplicates(clean_ingest_range):
    engine, before = clean_ingest_range

    # Three distinct events -> three new rows, counter advances by exactly 3.
    for n in (1, 2, 3):
        with engine.begin() as conn:
            assert ingest_event(conn, _event(n), _NO_TRIGGER) is None
    with engine.connect() as conn:
        after = int(conn.execute(text("SELECT ingested FROM ingest_state")).scalar())
        rows = conn.execute(
            text("SELECT count(*) FROM orders WHERE order_id >= :b AND order_id < :e"),
            {"b": _BASE_ID, "e": _BASE_ID + 1000},
        ).scalar()
    assert after - before["ingested"] == 3
    assert rows == 3

    # Redeliver event #1: no new row, counter does not move (idempotent).
    with engine.begin() as conn:
        assert ingest_event(conn, _event(1), _NO_TRIGGER) is None
    with engine.connect() as conn:
        after_dup = int(
            conn.execute(text("SELECT ingested FROM ingest_state")).scalar()
        )
        rows_dup = conn.execute(
            text("SELECT count(*) FROM orders WHERE order_id >= :b AND order_id < :e"),
            {"b": _BASE_ID, "e": _BASE_ID + 1000},
        ).scalar()
    assert after_dup == after
    assert rows_dup == 3


def test_ingest_returns_tumbling_window_on_trigger(clean_ingest_range):
    engine, _ = clean_ingest_range
    # Start from a known clean counter so the window bounds are deterministic.
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE ingest_state SET ingested=0, triggers_fired=0, window_lo=0 WHERE id")
        )

    threshold = 3
    results = []
    for n in (1, 2, 3, 4, 5, 6):
        with engine.begin() as conn:
            results.append(ingest_event(conn, _event(n), threshold))

    # First two and last two before each boundary return None; boundaries at the
    # 3rd and 6th event return successive, non-overlapping windows.
    assert results[0] is None and results[1] is None
    assert results[2] == {"lo": 0, "hi": _BASE_ID + 3}
    assert results[3] is None and results[4] is None
    assert results[5] == {"lo": _BASE_ID + 3, "hi": _BASE_ID + 6}
