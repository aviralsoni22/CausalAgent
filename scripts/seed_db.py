"""Seed the local Postgres data mart with synthetic e-commerce data.

Creates the schema defined in ``app.core.schema_def`` and populates it with
data that contains a *real*, recoverable treatment effect: orders that received
a discount have a genuinely higher expected total_amount. This gives the
SQL -> R -> evaluate pipeline something statistically meaningful to find.

Run (with the Postgres container up and your venv active):

    uv run python -m scripts.seed_db
"""
from __future__ import annotations

import random
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import text

from app.core import config
from app.core.db import get_engine
from app.core.schema_def import DDL
from app.sim import effects

# Tables the read-only analytics role is allowed to SELECT — exactly the three
# the SQL agent needs, and nothing else.
_ANALYTICS_TABLES = ("customers", "orders", "marketing_exposures")

N_CUSTOMERS = 3000
RNG_SEED = 42


def _build_frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    rng = random.Random(RNG_SEED)
    base_day = date(2024, 1, 1)

    customers, orders, exposures = [], [], []

    for cid in range(1, N_CUSTOMERS + 1):
        # One shared generative model (confounded multi-treatment, planted
        # effects) so the seeded mart matches the streamed mart exactly.
        unit = effects.generate_unit(cid, rng)
        customers.append(
            {
                "customer_id": unit["customer_id"],
                "signup_date": base_day + timedelta(days=rng.randint(0, 600)),
                "region": unit["region"],
                "age": unit["age"],
                "loyalty_tier": unit["loyalty_tier"],
            }
        )
        orders.append(
            {
                "order_id": unit["order_id"],
                "customer_id": unit["customer_id"],
                "order_date": base_day + timedelta(days=rng.randint(0, 600)),
                "received_discount": unit["received_discount"],
                "discount_pct": unit["discount_pct"],
                "ui_variant_b": unit["ui_variant_b"],
                "saw_banner": unit["saw_banner"],
                "total_amount": unit["total_amount"],
                "num_items": unit["num_items"],
            }
        )
        exposures.append(
            {
                "exposure_id": cid,
                "customer_id": cid,
                "campaign_id": f"CAMP-{rng.randint(1, 5)}",
                "exposed": 1 if rng.random() < 0.6 else 0,
                "exposure_date": base_day + timedelta(days=rng.randint(0, 600)),
            }
        )

    return (
        pd.DataFrame(customers),
        pd.DataFrame(orders),
        pd.DataFrame(exposures),
    )


def grant_readonly_role(engine) -> None:
    """Create/refresh the least-privilege role the SQL agent connects as.

    Idempotent. Granted SELECT on exactly the three analytics tables and nothing
    else — not the audit (``analysis_runs``) or ingest-state tables, no writes,
    no superuser functions. Run as an owner/admin connection (the seed engine).
    """
    role = config.READONLY_DB_USER
    if not role.isidentifier():
        raise ValueError(f"READONLY_DB_USER {role!r} is not a valid SQL identifier.")
    # DDL can't take bind params; the role name is identifier-validated above and
    # the password literal is escaped by doubling single quotes.
    pw_literal = "'" + config.READONLY_DB_PASSWORD.replace("'", "''") + "'"

    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
        ).scalar()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(text(f'{verb} ROLE "{role}" LOGIN PASSWORD {pw_literal}'))
        # Reset to a clean slate, then grant only what the extraction query needs.
        conn.execute(text(f'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "{role}"'))
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{config.DB_NAME}" TO "{role}"'))
        conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        for table in _ANALYTICS_TABLES:
            conn.execute(text(f'GRANT SELECT ON {table} TO "{role}"'))

    print(
        f"Read-only role {role!r} ready: SELECT on {', '.join(_ANALYTICS_TABLES)} "
        f"only (no write, no other tables)."
    )


def main() -> None:
    engine = get_engine()
    customers, orders, exposures = _build_frames()

    with engine.begin() as conn:
        for statement in DDL:
            conn.execute(text(statement))

    # Append into the freshly created tables (preserves PK/FK definitions).
    customers.to_sql("customers", engine, if_exists="append", index=False)
    orders.to_sql("orders", engine, if_exists="append", index=False)
    exposures.to_sql("marketing_exposures", engine, if_exists="append", index=False)

    planted = ", ".join(f"{k}={v}" for k, v in effects.TRUE_EFFECTS.items())
    print(
        f"Seeded {len(customers)} customers, {len(orders)} orders, "
        f"{len(exposures)} exposures. Planted ATEs (USD): {planted}."
    )

    # Provision the least-privilege role the SQL agent uses for extraction.
    grant_readonly_role(engine)


if __name__ == "__main__":
    main()
