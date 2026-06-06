"""The single source of truth for orchestrator state.

This TypedDict is the contract every LangGraph node reads from and writes to.
Per the architecture rules the LLM must NEVER see raw database rows — so the
state carries only the *path* to the extracted CSV and the column names, never
the data itself.
"""
from typing import TypedDict, Optional, List, Dict, Any


class CausalGraphState(TypedDict):
    task_id: str
    user_query: str
    # Explicit, auditable causal identification: which column is the treatment,
    # which is the outcome, and which are confounders to adjust for. Decided
    # once (by the user or the SQL agent) and obeyed by the R agent — so the
    # identification strategy is inspectable instead of buried inside an opaque
    # LLM-generated R script. Shape: {"treatment": str, "outcome": str,
    # "confounders": List[str]}.
    analysis_spec: Optional[dict]
    # Optional tumbling-window filter for the event-driven layer (Phase 3):
    # {"lo": int, "hi": int} restricts the analysis to orders whose order_id is
    # in (lo, hi]. None means analyse the whole table (the synchronous API path).
    window: Optional[dict]
    sql_query: Optional[str]
    data_file_path: Optional[str]
    extracted_columns: Optional[List[str]]
    r_script: Optional[str]
    statistical_output: Optional[dict]  # {"p_value", "ate", "is_significant", "method", "n_used", "max_smd", "balanced"}
    business_narrative: Optional[str]
    # Deterministic, plain-language statement of HOW the question was interpreted
    # — the effect of which treatment on which outcome, adjusting for which
    # confounders, over how many observations. Built from analysis_spec (not the
    # LLM) so the user can verify we answered the question they actually meant.
    interpretation: Optional[str]
    errors: List[str]
    # Total failures across all nodes — drives the human-readable fallback
    # message and is a quick "how hard did this fight" signal in provenance.
    retry_count: int
    # Per-node failure counts, keyed by node name ("sql_agent", "r_agent",
    # "executor", ...). Each node gets its OWN MAX_RETRIES budget, so a single
    # hiccup at each of several stages no longer exhausts one shared counter;
    # exhaustion is decided per node, not globally.
    retries: Dict[str, int]
    current_status: str
