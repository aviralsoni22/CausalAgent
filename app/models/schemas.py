"""Pydantic schemas for forced-structured LLM outputs.

Architecture Rule 4: every LLM call is bound with `.with_structured_output()`
to one of these models, so the orchestrator never has to parse free-form prose.
"""
from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class AnalysisSpec(BaseModel):
    """Explicit causal identification — the roles each column plays.

    Making this a first-class, structured artifact (rather than letting the R
    agent infer roles from column names) is what makes the identification
    strategy auditable and overridable.
    """

    treatment: str = Field(
        ..., description="The binary (0/1) treatment column whose effect we estimate."
    )
    outcome: str = Field(
        ..., description="The numeric outcome column the treatment is hypothesised to affect."
    )
    confounders: List[str] = Field(
        default_factory=list,
        description=(
            "Columns to adjust for as confounders — variables that plausibly "
            "affect both the treatment and the outcome. Empty only if none apply."
        ),
    )


class SQLGeneration(BaseModel):
    """What the SQL agent must return."""

    sql_query: str = Field(
        ...,
        description="A single, read-only SELECT statement against the e-commerce schema.",
    )
    spec: AnalysisSpec = Field(
        ...,
        description=(
            "The causal identification for this query. Every column named here "
            "(treatment, outcome, confounders) MUST be projected by sql_query."
        ),
    )
    reasoning: str = Field(
        ...,
        description="One or two sentences explaining how this query and identification answer the user's question.",
    )


class RScriptGeneration(BaseModel):
    """What the R-stats agent must return."""

    r_script: str = Field(
        ...,
        description=(
            "A complete, self-contained R script. It reads the CSV path from "
            "the DATA_FILE_PATH environment variable, handles NA values, "
            "adjusts for confounders (MatchIt propensity-score matching for a "
            "binary treatment, else covariate-adjusted lm), and prints exactly "
            'one JSON object to stdout with keys "p_value", "ate", "method", '
            '"n_used", and "max_smd" (post-match balance, null when not matched).'
        ),
    )


class BusinessNarrative(BaseModel):
    """What the reviewer agent must return."""

    business_narrative: str = Field(
        ...,
        description="A concise, non-technical explanation of the statistical result for a business stakeholder.",
    )
