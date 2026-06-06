"""Run one full causal-analysis task through the compiled graph (no Celery).

Drives the same ``compiled_graph`` the Celery worker uses, but synchronously and
from the CLI, so you can exercise the entire SQL -> R -> evaluate -> review
pipeline against the live Anthropic API + running R sandbox in one command.

Requires: ANTHROPIC_API_KEY in .env, Postgres up and seeded, r_sandbox up.

Run:
    .venv/Scripts/python.exe -m scripts.run_task
    .venv/Scripts/python.exe -m scripts.run_task "Did discounts raise order totals?"
"""
from __future__ import annotations

import json
import sys
import uuid

from app.core import config
from app.core.cleanup import purge_extracted_data
from app.core.graph import compiled_graph, initial_state
from app.core.observability import configure_tracing

DEFAULT_QUERY = (
    "Did receiving a discount cause customers to spend more per order? "
    "Adjust for customer age and region."
)


def main(argv: list[str]) -> int:
    if not config.ANTHROPIC_API_KEY:
        print("ANTHROPIC_API_KEY is empty. Add it to .env, then re-run.", file=sys.stderr)
        return 1

    tracing = configure_tracing()
    print(f"tracing : {'on (LangSmith)' if tracing else 'off'}")

    query = argv[1] if len(argv) > 1 else DEFAULT_QUERY
    task_id = uuid.uuid4().hex[:12]

    print(f"task_id : {task_id}")
    print(f"model   : {config.ANTHROPIC_MODEL}")
    print(f"query   : {query}\n")
    print("Running graph (sql -> r -> execute -> evaluate -> review) ...\n")

    state = initial_state(task_id=task_id, user_query=query)
    try:
        final = compiled_graph.invoke(state, config={"recursion_limit": 50})
    finally:
        # Don't leave the extracted customer rows on disk after the run.
        purge_extracted_data(task_id)

    print("=" * 70)
    print(f"status          : {final.get('current_status')}")
    print(f"retry_count     : {final.get('retry_count')}")
    print(f"\nsql_query       :\n{final.get('sql_query')}")
    print(f"\nextracted_cols  : {final.get('extracted_columns')}")
    print(f"data_file_path  : {final.get('data_file_path')}")
    print(f"\nstatistical_out :\n{json.dumps(final.get('statistical_output'), indent=2)}")
    print(f"\nbusiness_narrative:\n{final.get('business_narrative')}")
    if final.get("errors"):
        print(f"\nerrors          :")
        for e in final["errors"]:
            print(f"  - {e}")
    print("=" * 70)

    return 0 if final.get("current_status") == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
