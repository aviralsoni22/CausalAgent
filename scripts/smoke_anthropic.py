"""Live Anthropic API smoke test.

Exercises the exact path the SQL agent uses: ``get_llm()`` bound to the real
``SQLGeneration`` schema via ``.with_structured_output()``. A pass proves the
API key, model name, ``langchain_anthropic`` wiring, and structured-output
round-trip all work end to end -- without needing Docker, Postgres, or the R
sandbox.

Run:
    .venv/Scripts/python.exe -m scripts.smoke_anthropic
"""
from __future__ import annotations

import sys

from app.core import config
from app.core.llm import get_llm
from app.models.schemas import SQLGeneration


PROMPT = (
    "You are a SQL + causal-inference agent for an e-commerce data mart. "
    "Question: Did enrolling customers in the loyalty program increase their "
    "30-day spend? Propose a single read-only SELECT and the causal "
    "identification (treatment, outcome, confounders)."
)


def main() -> int:
    if not config.ANTHROPIC_API_KEY:
        print(
            "ANTHROPIC_API_KEY is empty. Add your key to .env, then re-run.",
            file=sys.stderr,
        )
        return 1

    print(f"Calling Anthropic model: {config.ANTHROPIC_MODEL} ...")
    structured = get_llm().with_structured_output(SQLGeneration)
    result: SQLGeneration = structured.invoke(PROMPT)

    print("\n--- Structured response (SQLGeneration) ---")
    print(f"sql_query : {result.sql_query}")
    print(f"treatment : {result.spec.treatment}")
    print(f"outcome   : {result.spec.outcome}")
    print(f"confounders: {result.spec.confounders}")
    print(f"reasoning : {result.reasoning}")
    print("\nOK: Anthropic API + structured output round-trip succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
