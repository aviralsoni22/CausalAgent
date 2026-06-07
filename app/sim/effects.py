"""The generative model behind every demo: customers and orders with several
treatments, each carrying a *known, planted* causal effect on ``total_amount``.

This is the single source of truth for the simulation. Both ``scripts/seed_db``
(bulk seed) and ``scripts/stream_events`` / the ``/sim`` routes (live stream)
generate rows through ``generate_unit`` so the seeded mart and the streamed mart
share one model — and the ground-truth card in ``/sim/truth`` is rendered from
``TRUE_EFFECTS`` here, never a hard-coded copy elsewhere.

Two properties make this a *causal* demo rather than a stats demo:

1. Each treatment's assignment probability depends on a covariate that ALSO
   moves the outcome (confounding), so the naive difference-in-means is biased
   and the agent's adjustment is what recovers the truth.
2. ``saw_banner`` is a PLACEBO — its true effect is exactly 0, but it is
   correlated with ``loyalty_tier`` (gold customers see it more and spend more),
   so a naive look shows a spurious lift the agent must explain away.
"""
from __future__ import annotations

import random

REGIONS = ["NA", "EU", "APAC", "LATAM"]
TIERS = ["bronze", "silver", "gold"]

# Covariate effects on the outcome (USD). These are what create confounding when
# paired with covariate-dependent treatment assignment below.
REGION_EFFECT = {"NA": 10.0, "EU": 6.0, "APAC": 4.0, "LATAM": 0.0}
TIER_EFFECT = {"bronze": 0.0, "silver": 5.0, "gold": 12.0}
AGE_EFFECT = 0.4  # USD per year

# The planted causal effects the agent must recover. saw_banner is the placebo.
TRUE_EFFECTS: dict[str, float] = {
    "received_discount": 14.0,
    "ui_variant_b": 6.0,
    "saw_banner": 0.0,
}

# Human-readable confounder for each treatment — drives both the data model and
# the interviewer-facing truth card.
CONFOUNDER = {
    "received_discount": "age",
    "ui_variant_b": "region",
    "saw_banner": "loyalty_tier",
}

# Baseline assignment propensities (before any campaign "focus" boost). Each is a
# function of the treatment's confounder, so treated and untreated groups differ
# systematically — exactly what the agent has to correct for.
_VARIANT_PROB = {"NA": 0.6, "EU": 0.5, "APAC": 0.4, "LATAM": 0.3}
_BANNER_PROB = {"bronze": 0.30, "silver": 0.50, "gold": 0.75}

# A campaign button raises its treatment's uptake without removing confounding
# (assignment still depends on the covariate) and without erasing the control
# group (baseline batches keep emitting untreated units).
_FOCUS_BOOST = 0.35


def _clamp01(p: float) -> float:
    return max(0.0, min(1.0, p))


def generate_unit(unit_id: int, rng: random.Random, focus: str | None = None) -> dict:
    """One customer + their order, with all treatments confounded-assigned.

    ``focus`` (a key of ``TRUE_EFFECTS``) boosts that treatment's prevalence for
    this unit — the "run a campaign" lever — while preserving its covariate
    dependence. Returns a flat dict with every customers/orders column the seed,
    the stream, and the consumer need.
    """
    age = rng.randint(18, 70)
    region = rng.choice(REGIONS)
    tier = rng.choice(TIERS)

    # Confounded assignment: older -> more likely discounted; some regions push
    # variant B; higher tiers see the banner more often.
    p_discount = _clamp01(
        0.15 + 0.012 * (age - 18) + (_FOCUS_BOOST if focus == "received_discount" else 0.0)
    )
    p_variant = _clamp01(
        _VARIANT_PROB[region] + (_FOCUS_BOOST if focus == "ui_variant_b" else 0.0)
    )
    p_banner = _clamp01(
        _BANNER_PROB[tier] + (_FOCUS_BOOST if focus == "saw_banner" else 0.0)
    )

    received_discount = 1 if rng.random() < p_discount else 0
    ui_variant_b = 1 if rng.random() < p_variant else 0
    saw_banner = 1 if rng.random() < p_banner else 0
    discount_pct = round(rng.uniform(5, 25), 2) if received_discount else 0.0

    total = (
        50.0
        + TRUE_EFFECTS["received_discount"] * received_discount
        + TRUE_EFFECTS["ui_variant_b"] * ui_variant_b
        + TRUE_EFFECTS["saw_banner"] * saw_banner  # 0.0 — the placebo adds nothing
        + AGE_EFFECT * age
        + REGION_EFFECT[region]
        + TIER_EFFECT[tier]
        + rng.gauss(0, 8)
    )

    return {
        "customer_id": unit_id,
        "order_id": unit_id,
        "age": age,
        "region": region,
        "loyalty_tier": tier,
        "received_discount": received_discount,
        "discount_pct": discount_pct,
        "ui_variant_b": ui_variant_b,
        "saw_banner": saw_banner,
        "total_amount": round(max(total, 0.0), 2),
        "num_items": rng.randint(1, 8),
    }


# The OrderEvent constructor fields a generate_unit() dict maps onto. Lives here
# (the generator's home) so both the CLI streamer and the /sim routes project
# units the same way without app code reaching into scripts/.
ORDER_EVENT_KEYS = (
    "order_id", "customer_id", "age", "region", "loyalty_tier",
    "received_discount", "discount_pct", "ui_variant_b", "saw_banner",
    "total_amount", "num_items",
)


def event_fields(unit: dict) -> dict:
    """Project a generate_unit() dict onto OrderEvent's constructor fields."""
    return {k: unit[k] for k in ORDER_EVENT_KEYS}


def naive_estimate(rows, treatment: str, outcome: str = "total_amount") -> float | None:
    """Unadjusted difference in means — the biased number the agent corrects.

    Accepts a list of dicts or a pandas DataFrame. Returns None if either arm is
    empty (no contrast to estimate).
    """
    treated = [r[outcome] for r in _iter_rows(rows) if r[treatment] == 1]
    control = [r[outcome] for r in _iter_rows(rows) if r[treatment] == 0]
    if not treated or not control:
        return None
    return sum(treated) / len(treated) - sum(control) / len(control)


def _iter_rows(rows):
    """Yield row dicts from either a list of dicts or a pandas DataFrame."""
    to_dict = getattr(rows, "to_dict", None)
    if to_dict is not None:  # DataFrame
        return to_dict(orient="records")
    return rows
