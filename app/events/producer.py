"""Kafka producer for order events.

Thin wrapper over ``KafkaProducer`` that serialises ``OrderEvent`` models to
JSON. The connection settings come from ``config.kafka_client_kwargs`` so the
same code publishes to local Redpanda and to a managed broker.
"""
from __future__ import annotations

import json

from kafka import KafkaProducer

from app.core import config
from app.events.schemas import OrderEvent


def build_producer() -> KafkaProducer:
    return KafkaProducer(
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        # Wait for the leader to ack so a demo run can't silently drop events.
        acks=1,
        **config.kafka_client_kwargs(),
    )


def publish(producer: KafkaProducer, event: OrderEvent) -> None:
    producer.send(config.KAFKA_TOPIC, value=event.model_dump())
