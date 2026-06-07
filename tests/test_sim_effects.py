"""Tests for the simulation generative model.

These guard the two properties the demo depends on: the placebo is truly zero,
and treatment assignment is confounded so the naive estimate is biased away from
the planted truth (which is exactly what the causal agent has to correct).
Recovering the *adjusted* estimate is the R pipeline's job, exercised end-to-end
elsewhere — here we only assert the data has the structure that makes that
recovery meaningful.
"""
from __future__ import annotations

import random

from app.events.schemas import OrderEvent
from app.sim import effects


def _frame(n: int, seed: int = 42, focus: str | None = None) -> list[dict]:
    rng = random.Random(seed)
    return [effects.generate_unit(i, rng, focus=focus) for i in range(1, n + 1)]


def test_generate_unit_is_deterministic_for_a_seed():
    a = effects.generate_unit(1, random.Random(123))
    b = effects.generate_unit(1, random.Random(123))
    assert a == b


def test_generated_unit_validates_as_an_order_event():
    unit = effects.generate_unit(1, random.Random(1))
    event = OrderEvent(order_date="2025-01-01", **effects.event_fields(unit))
    assert event.ui_variant_b in (0, 1)
    assert event.saw_banner in (0, 1)
    assert event.loyalty_tier in effects.TIERS


def test_placebo_true_effect_is_zero():
    assert effects.TRUE_EFFECTS["saw_banner"] == 0.0


def test_naive_estimate_matches_manual_difference_in_means():
    rows = [
        {"t": 1, "y": 10.0},
        {"t": 1, "y": 20.0},
        {"t": 0, "y": 4.0},
        {"t": 0, "y": 6.0},
    ]
    assert effects.naive_estimate(rows, "t", "y") == 15.0 - 5.0


def test_naive_estimate_is_none_without_both_arms():
    assert effects.naive_estimate([{"t": 1, "y": 1.0}], "t", "y") is None


def test_discount_naive_estimate_is_biased_upward_by_age_confounding():
    rows = _frame(6000)
    naive = effects.naive_estimate(rows, "received_discount")
    # Older customers are likelier to be discounted AND spend more, so the
    # unadjusted contrast overshoots the planted +14 ATE.
    assert naive > effects.TRUE_EFFECTS["received_discount"] + 1.5


def test_placebo_shows_spurious_positive_naive_estimate():
    rows = _frame(6000)
    naive = effects.naive_estimate(rows, "saw_banner")
    # True effect is 0, but loyalty-tier correlation manufactures a fake lift —
    # the trap the agent must see through. Direction and presence, not exactness.
    assert naive > 2.0
