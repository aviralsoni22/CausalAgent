"""Centralised configuration, sourced entirely from the environment.

Architecture Rule 5: hardcoding `localhost` inside application code is
forbidden. Every hostname, port, credential and URL is read here from env vars
(loaded from `.env` in local dev) so the same image runs unchanged on K8s.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


# --- LLM ------------------------------------------------------------------
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
# Model is env-configurable. The roadmap names "Claude 3.5"; we default to a
# current production model and let ops override per environment.
ANTHROPIC_MODEL: str = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")

# --- Postgres (the enterprise data mart) ----------------------------------
DB_HOST: str = os.environ.get("DB_HOST", "localhost")
DB_PORT: str = os.environ.get("DB_PORT", "5432")
DB_USER: str = os.environ.get("DB_USER", "admin")
DB_PASSWORD: str = os.environ.get("DB_PASSWORD", "password")
DB_NAME: str = os.environ.get("DB_NAME", "enterprise_dw")


def database_url() -> str:
    return (
        f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}"
        f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    )


# Connection-pool tuning for the shared engine (see app/core/db.py).
DB_POOL_SIZE: int = int(os.environ.get("DB_POOL_SIZE", "5"))
DB_MAX_OVERFLOW: int = int(os.environ.get("DB_MAX_OVERFLOW", "10"))
DB_POOL_RECYCLE: int = int(os.environ.get("DB_POOL_RECYCLE", "1800"))


# --- Celery / Redis -------------------------------------------------------
REDIS_HOST: str = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT: str = os.environ.get("REDIS_PORT", "6379")
CELERY_BROKER_URL: str = os.environ.get(
    "CELERY_BROKER_URL", f"redis://{REDIS_HOST}:{REDIS_PORT}/0"
)
CELERY_RESULT_BACKEND: str = os.environ.get(
    "CELERY_RESULT_BACKEND", f"redis://{REDIS_HOST}:{REDIS_PORT}/1"
)

# --- R sandbox ------------------------------------------------------------
R_SANDBOX_URL: str = os.environ.get("R_SANDBOX_URL", "http://localhost:8001")
R_SANDBOX_TIMEOUT: int = int(os.environ.get("R_SANDBOX_TIMEOUT", "150"))

# --- Local data ------------------------------------------------------------
# Where the SQL agent writes extracted CSVs. The executor reads the file back
# and ships its content to the sandbox in the request body, so this is a purely
# local working directory — no shared volume with the sandbox is required.
DATA_DIR: str = os.environ.get("DATA_DIR", "data")

# --- Orchestration --------------------------------------------------------
MAX_RETRIES: int = int(os.environ.get("MAX_RETRIES", "3"))


# --- Kafka / event-driven layer (Phase 3) ---------------------------------
# Local dev points at the Redpanda container's external listener. In deployment
# this becomes the managed-Kafka bootstrap server and the SASL/TLS settings
# below are filled in — no application code changes, only env.
KAFKA_BOOTSTRAP_SERVERS: str = os.environ.get(
    "KAFKA_BOOTSTRAP_SERVERS", "localhost:19092"
)
KAFKA_TOPIC: str = os.environ.get("KAFKA_TOPIC", "order-events")
KAFKA_CONSUMER_GROUP: str = os.environ.get("KAFKA_CONSUMER_GROUP", "causal-consumer")
# How many ingested events trigger one automatic causal analysis. Kept high so a
# demo fires the (LLM + R) pipeline deliberately, not on every message.
KAFKA_TRIGGER_THRESHOLD: int = int(os.environ.get("KAFKA_TRIGGER_THRESHOLD", "500"))

# Auth: PLAINTEXT for local Redpanda; SASL_SSL + SCRAM/PLAIN for managed Kafka.
KAFKA_SECURITY_PROTOCOL: str = os.environ.get("KAFKA_SECURITY_PROTOCOL", "PLAINTEXT")
KAFKA_SASL_MECHANISM: str = os.environ.get("KAFKA_SASL_MECHANISM", "")
KAFKA_SASL_USERNAME: str = os.environ.get("KAFKA_SASL_USERNAME", "")
KAFKA_SASL_PASSWORD: str = os.environ.get("KAFKA_SASL_PASSWORD", "")


def kafka_client_kwargs() -> dict:
    """Connection kwargs shared by the producer and consumer.

    Returns only the security args that are actually configured, so the same
    code path serves local PLAINTEXT (no auth) and managed SASL_SSL brokers.
    """
    kwargs: dict = {
        "bootstrap_servers": KAFKA_BOOTSTRAP_SERVERS.split(","),
        "security_protocol": KAFKA_SECURITY_PROTOCOL,
    }
    if KAFKA_SASL_MECHANISM:
        kwargs["sasl_mechanism"] = KAFKA_SASL_MECHANISM
        kwargs["sasl_plain_username"] = KAFKA_SASL_USERNAME
        kwargs["sasl_plain_password"] = KAFKA_SASL_PASSWORD
    return kwargs
