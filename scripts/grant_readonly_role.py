"""Create/refresh the least-privilege read-only DB role, without re-seeding.

The SQL agent runs LLM-generated (and possibly prompt-injected) SELECTs, so it
connects as a role that can read only the analytics tables — never write, never
reach other tables, never call superuser functions. ``scripts.seed_db`` already
provisions this role on a fresh seed; this standalone entry point applies the
same grant to an existing database without dropping data.

Run as an owner/admin connection (the default DB_USER), with the container up:

    uv run python -m scripts.grant_readonly_role
"""
from __future__ import annotations

from app.core.db import get_engine
from scripts.seed_db import grant_readonly_role

if __name__ == "__main__":
    grant_readonly_role(get_engine())
