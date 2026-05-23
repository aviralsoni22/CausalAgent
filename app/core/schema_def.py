"""The canonical e-commerce data-mart schema.

Defined once and shared by (a) the SQL agent, which injects ``SCHEMA_PROMPT``
into the LLM prompt, and (b) ``scripts/seed_db.py``, which uses ``DDL`` and the
seed routine to populate a runnable local database. Keeping both in one module
guarantees the prompt can never drift from the real tables.
"""
from __future__ import annotations

# Human-readable schema injected into the SQL agent prompt. Deliberately terse
# and column-typed so the LLM writes valid SELECTs without seeing any rows.
SCHEMA_PROMPT = """
Postgres schema (database: enterprise_dw). All tables are read-only.

customers(
    customer_id      INTEGER PRIMARY KEY,
    signup_date      DATE,
    region           TEXT,            -- one of: 'NA', 'EU', 'APAC', 'LATAM'
    age              INTEGER,
    loyalty_tier     TEXT             -- one of: 'bronze', 'silver', 'gold'
)

orders(
    order_id         INTEGER PRIMARY KEY,
    customer_id      INTEGER REFERENCES customers(customer_id),
    order_date       DATE,
    received_discount INTEGER,        -- treatment flag: 1 if a discount was applied, else 0
    discount_pct     NUMERIC,         -- percentage discount applied (0 when received_discount = 0)
    total_amount     NUMERIC,         -- final order value in USD (the typical outcome variable)
    num_items        INTEGER
)

marketing_exposures(
    exposure_id      INTEGER PRIMARY KEY,
    customer_id      INTEGER REFERENCES customers(customer_id),
    campaign_id      TEXT,
    exposed          INTEGER,         -- 1 if the customer saw the campaign, else 0
    exposure_date    DATE
)
""".strip()


# DDL used by the seed script to create the tables.
DDL = [
    "DROP TABLE IF EXISTS marketing_exposures CASCADE;",
    "DROP TABLE IF EXISTS orders CASCADE;",
    "DROP TABLE IF EXISTS customers CASCADE;",
    """
    CREATE TABLE customers (
        customer_id   INTEGER PRIMARY KEY,
        signup_date   DATE,
        region        TEXT,
        age           INTEGER,
        loyalty_tier  TEXT
    );
    """,
    """
    CREATE TABLE orders (
        order_id          INTEGER PRIMARY KEY,
        customer_id       INTEGER REFERENCES customers(customer_id),
        order_date        DATE,
        received_discount INTEGER,
        discount_pct      NUMERIC,
        total_amount      NUMERIC,
        num_items         INTEGER
    );
    """,
    """
    CREATE TABLE marketing_exposures (
        exposure_id    INTEGER PRIMARY KEY,
        customer_id    INTEGER REFERENCES customers(customer_id),
        campaign_id    TEXT,
        exposed        INTEGER,
        exposure_date  DATE
    );
    """,
]
