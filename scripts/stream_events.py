"""Simulate a live clickstream of order events onto Kafka.

Generates synthetic orders through the shared generative model in
``app.sim.effects`` — the same confounded, multi-treatment model the seed uses —
so when the consumer crosses its threshold and fires the causal pipeline, the
analysis recovers the planted ATEs.

``--focus`` boosts one treatment's uptake (a "campaign") without removing its
confounding or erasing the control group; omit it for a baseline mix.

IDs start at a high offset so streamed rows never collide with seeded data.

Run (with Redpanda up):
    .venv/Scripts/python.exe -m scripts.stream_events --count 600
    .venv/Scripts/python.exe -m scripts.stream_events --count 600 --focus received_discount
"""
from __future__ import annotations

import argparse
import random

from app.core import config
from app.events.producer import build_producer, publish
from app.events.schemas import OrderEvent
from app.sim import effects

ID_OFFSET = 100_000


def _make_event(seq: int, rng: random.Random, focus: str | None) -> OrderEvent:
    unit = effects.generate_unit(ID_OFFSET + seq, rng, focus=focus)
    return OrderEvent(order_date="2025-01-01", **effects.event_fields(unit))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--count",
        type=int,
        default=config.KAFKA_TRIGGER_THRESHOLD + 100,
        help="Number of order events to publish.",
    )
    parser.add_argument(
        "--focus",
        choices=sorted(effects.TRUE_EFFECTS),
        default=None,
        help="Boost one treatment's uptake (simulate a campaign).",
    )
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    producer = build_producer()
    for seq in range(1, args.count + 1):
        publish(producer, _make_event(seq, rng, args.focus))
    producer.flush()
    producer.close()
    planted = ", ".join(f"{k}={v}" for k, v in effects.TRUE_EFFECTS.items())
    print(
        f"Published {args.count} order events to topic '{config.KAFKA_TOPIC}'"
        f"{f' (focus={args.focus})' if args.focus else ''}. Planted ATEs: {planted}."
    )


if __name__ == "__main__":
    main()
