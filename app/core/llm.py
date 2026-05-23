"""Anthropic LLM factory.

A single place that constructs the chat model so every agent shares identical
configuration. Agents call `get_llm()` and then `.with_structured_output(...)`
to satisfy Architecture Rule 4 (all LLM outputs forced into Pydantic schemas).
"""
from __future__ import annotations

from functools import lru_cache

from langchain_anthropic import ChatAnthropic

from app.core import config


@lru_cache(maxsize=1)
def get_llm() -> ChatAnthropic:
    """Return a process-wide singleton chat model.

    Temperature is 0 because this platform generates SQL and statistical code:
    we want deterministic, reproducible output, not creative variation.
    """
    if not config.ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to your .env before running agents."
        )
    return ChatAnthropic(
        model=config.ANTHROPIC_MODEL,
        api_key=config.ANTHROPIC_API_KEY,
        temperature=0,
        max_tokens=4096,
        timeout=120,
    )
