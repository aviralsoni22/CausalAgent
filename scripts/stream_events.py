"""Simulate a live clickstream of order events onto Kafka.

Generates synthetic orders that reproduce the same generative model as the seed
script (a real, recoverable discount effect of ~14 USD on order total, with age
and region as covariates), so when the consumer crosses its threshold and fires
the causal pipeline, the analysis recovers a meaningful ATE.

IDs start at a high offset so streamed rows never collide with seeded data.

Run (with Redpanda up):
    .venv/Scripts/python.exe -m scripts.stream_events --count 600
"""
from __future__ import annotations

import argparse
import random

from app.core import config
from app.events.producer import build_producer, publish
from app.events.schemas import OrderEvent

TRUE_DISCOUNT_ATE = 14.0
REGIONS = ["NA", "EU", "APAC", "LATAM"]
REGION_EFFECT = {"NA": 10.0, "EU": 6.0, "APAC": 4.0, "LATAM": 0.0}
ID_OFFSET = 100_000


def _make_event(seq: int, rng: random.Random) -> OrderEvent:
    age = rng.randint(18, 70)
    region = rng.choice(REGIONS)
    received_discount = 1 if rng.random() < 0.5 else 0
    discount_pct = round(rng.uniform(5, 25), 2) if received_discount else 0.0
    total = (
        50.0
        + TRUE_DISCOUNT_ATE * received_discount
        + 0.4 * age
        + REGION_EFFECT[region]
        + rng.gauss(0, 8)
    )
    return OrderEvent(
        order_id=ID_OFFSET + seq,
        customer_id=ID_OFFSET + seq,
        order_date="2025-01-01",
        age=age,
        region=region,
        received_discount=received_discount,
        discount_pct=discount_pct,
        total_amount=round(max(total, 0.0), 2),
        num_items=rng.randint(1, 8),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--count",
        type=int,
        default=config.KAFKA_TRIGGER_THRESHOLD + 100,
        help="Number of order events to publish.",
    )
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    producer = build_producer()
    for seq in range(1, args.count + 1):
        publish(producer, _make_event(seq, rng))
    producer.flush()
    producer.close()
    print(
        f"Published {args.count} order events to topic '{config.KAFKA_TOPIC}' "
        f"(true ATE = {TRUE_DISCOUNT_ATE} USD)."
    )


if __name__ == "__main__":
    main()
