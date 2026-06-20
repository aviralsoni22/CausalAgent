# CausalAgent — Engineering Report

*A full account of what was built, how it was built, and why each significant
decision was made.*

---

## Table of contents

1. [What CausalAgent is](#1-what-causalagent-is)
2. [The core problem and the guiding philosophy](#2-the-core-problem-and-the-guiding-philosophy)
3. [The five architecture rules](#3-the-five-architecture-rules)
4. [How the system is shaped (end to end)](#4-how-the-system-is-shaped-end-to-end)
5. [Build history, phase by phase](#5-build-history-phase-by-phase)
6. [Component deep dive](#6-component-deep-dive)
7. [The orchestrator: retries, recovery, and fallback](#7-the-orchestrator-retries-recovery-and-fallback)
8. [The event-driven layer](#8-the-event-driven-layer)
9. [Crucial decisions and what drove them](#9-crucial-decisions-and-what-drove-them)
10. [Statistical correctness: the part most demos skip](#10-statistical-correctness-the-part-most-demos-skip)
11. [Security posture](#11-security-posture)
12. [Testing strategy](#12-testing-strategy)
13. [Configuration and deployment story](#13-configuration-and-deployment-story)
14. [Honest assessment: limits and open risks](#14-honest-assessment-limits-and-open-risks)
15. [Glossary](#15-glossary)

---

## 1. What CausalAgent is

CausalAgent is a distributed, multi-language platform that turns a plain-English
business question — for example, *"Did giving customers a discount actually make
them spend more?"* — into a rigorous **causal inference** result, computed in R,
and then explains that result back in language a non-statistician can act on.

It is "polyglot" in a deliberate sense: **Python orchestrates, R does the
statistics.** Python (with a large language model) is excellent at understanding
a question and writing code; R is the lingua franca of applied statistics and
ships mature, peer-reviewed packages for the hard part — estimating an effect
while adjusting for confounding. The system plays each language to its strength
rather than forcing one to do everything.

The platform has two front doors that feed the **same** analysis engine:

- A **synchronous HTTP API** — a user submits a question and polls for the answer.
- An **event-driven stream layer** — a live feed of e-commerce orders accumulates,
  and once enough new data has arrived, an analysis fires automatically.

In front of those, a **Discord slash-command bot** (`app/bots/`) is the human
front door — a thin client over the HTTP API — and a **fake-storefront
simulation** (`app/sim/`) drives the event layer with confounded, ground-truth-
labelled data (including a true-null placebo) so the agent's answers are
*checkable* against planted effects. See Sections 10 and 11.

Both paths converge on one pipeline:

```
SQL agent  →  R agent  →  executor  →  evaluator  →  reviewer
(question   (writes the  (runs R in   (parses the   (writes the
 → SQL)      stats code)  a sandbox)   numbers)      plain-English
                                                     narrative)
```

---

## 2. The core problem and the guiding philosophy

### Why this is hard

A naive "AI data analyst" stuffs database rows into an LLM prompt and asks it to
reason about them. That approach fails on three fronts at once:

1. **It does not scale.** Real tables have millions of rows; a prompt holds
   thousands of tokens. The data never fits.
2. **It is not rigorous.** An LLM "reasoning" over numbers in prose produces
   plausible-sounding statistics, not correct ones. Causal questions in
   particular ("did X *cause* Y?") require formal adjustment for confounders —
   not pattern-matching on raw values.
3. **It is not safe.** Letting a model both read sensitive enterprise data and
   generate free-running code is a security and compliance problem.

### The philosophy that answers it

The whole design follows from one principle: **the LLM is a translator, not a
calculator.** It translates a question into SQL, translates a dataset into an R
script, and translates a number into a narrative. It never sees the raw data and
never does the arithmetic. The deterministic, auditable parts — query execution,
statistical estimation — are done by tools that are *good* at them (Postgres, R),
in isolation, with their outputs forced into strict schemas.

This single idea is what the five architecture rules below encode.

---

## 3. The five architecture rules

These rules were set as hard constraints up front (recorded in
`MASTER_PROMPT.md`). Every component was built to obey them. They are worth
stating precisely because almost every later decision traces back to one of them.

| # | Rule | Why it exists | Where it shows up |
|---|------|---------------|-------------------|
| 1 | **No RAG on tabular data.** No vector database. The SQL agent extracts data; the R agent writes deterministic math. | Vector search over rows is the wrong tool — it gives fuzzy retrieval where we need exact aggregation and modelling. | `sql_agent.py`, `r_agent.py` |
| 2 | **Context-window protection.** The LLM must never see raw database rows. Only the `data_file_path` and the column names move through the graph state. | Solves the "data doesn't fit / shouldn't leak" problem at the architectural level rather than by hoping prompts stay small. | `state.py`, `sql_agent.py` writes a CSV; only its path travels onward |
| 3 | **Secure polyglot execution.** Python's `exec`/`eval` are forbidden. R runs only inside an isolated Docker sandbox. | LLM-generated code is untrusted by definition. It must run somewhere it cannot reach credentials, the broker, or the model. | `sandbox/` service, `executor.py` |
| 4 | **Structured outputs.** Every LLM call is bound with `.with_structured_output()` to a strict Pydantic schema. | Eliminates fragile free-text parsing; the orchestrator always receives typed fields, never prose to scrape. | `models/schemas.py`, every agent |
| 5 | **Decoupled, deploy-anywhere design.** No hostnames, ports, or keys hardcoded — everything comes from environment variables. | The system is meant to graduate to a managed container platform (originally K8s; now a container PaaS, Section 13). Config-as-environment is what lets the same image run unchanged in dev, staging, and prod. | `config.py` is the single source of all external wiring |

---

## 4. How the system is shaped (end to end)

A single request travels like this:

1. **Ingress (`app/main.py`).** A `POST /analyze` arrives with a question. The
   API mints a `task_id`, enqueues a Celery job, and returns *immediately* with
   that id. It never runs the analysis itself — heavy LLM-plus-R work must not
   block the request thread. The client later polls `GET /status/{task_id}`.

2. **Queue (Celery + broker).** The job waits on a message broker until a worker
   is free.

3. **Worker (`app/worker.py`).** A Celery worker picks up the job and drives the
   compiled LangGraph orchestrator to completion.

4. **Orchestrator (`app/core/graph.py`).** A state machine over a typed
   `CausalGraphState` runs the five nodes in sequence, with conditional edges
   that handle failure and retry (Section 7).

5. **Sandbox (`sandbox/main.py`).** The executor node ships the generated R
   script and the data to an isolated container over HTTP; the container runs
   `Rscript` and returns text.

6. **Persistence (`app/core/persistence.py`).** The final state is written as one
   row to an `analysis_runs` audit table, so every result is reproducible.

The full picture — including the parallel event path — is drawn in
`docs/ARCHITECTURE.md`. This report focuses on the *reasoning*; that file holds
the diagrams.

### The state object is the contract

Everything hinges on one typed dictionary, `CausalGraphState` (`app/core/state.py`):

```python
class CausalGraphState(TypedDict):
    task_id: str
    user_query: str
    analysis_spec: Optional[dict]   # {"treatment", "outcome", "confounders"}
    window: Optional[dict]          # {"lo", "hi"} for the event path
    sql_query: Optional[str]
    data_file_path: Optional[str]   # path to the CSV, never the rows themselves
    extracted_columns: Optional[List[str]]
    r_script: Optional[str]
    statistical_output: Optional[dict]
    business_narrative: Optional[str]
    errors: List[str]
    retry_count: int                # total failures, for the fallback message
    retries: Dict[str, int]         # per-node failure counts
    current_status: str
```

Two fields deserve emphasis because they carry the architectural intent:

- `data_file_path` (not the data) is what flows forward — **Rule 2 made
  concrete.** The rows live on disk; the graph carries only a pointer.
- `analysis_spec` makes the **causal identification explicit and auditable.**
  Rather than letting the R agent silently guess which column is the "treatment"
  and which are "confounders," that decision is a first-class, inspectable field
  (more in Section 6).

---

## 5. Build history, phase by phase

The repository was built in clearly separable stages, visible in the git history.

| Commit | Stage | What landed |
|--------|-------|-------------|
| `b133a39` | **Phase 1 — async foundation** | The skeleton: `docker-compose.yml` (Postgres + Redis), an empty FastAPI ingress and Celery worker, `pyproject.toml` managed by `uv`. |
| `987005b` | **Pipeline wired up** | The whole engine in one stroke: all five agents, the LangGraph orchestrator, the R sandbox + Dockerfile, the Pydantic schemas, persistence, the seed script, and the first deterministic end-to-end test. |
| `99a1278` | **Phase 2 complete** | Operational scripts: `run_task.py` (drive the graph from the CLI), `smoke_anthropic.py` (verify the API key/model), `verify_sandbox.py` (verify the R container). |
| `adbd569` | **Sandbox decoupling** | Reworked data transport: the executor now sends the CSV *content* inline (base64) instead of relying on a shared volume — so the sandbox can live on a different host. |
| `df2c1fc` | **Phase 3 — event layer + per-node retries** | The Kafka/Redpanda producer and consumer, tumbling-window analysis triggering, and a rewrite of the retry model from one shared counter to an independent budget per node. |
| `2ce5ff2` | **Balance gate** | The R estimator now checks whether propensity-score matching actually balanced the covariates and falls back to covariate adjustment if it did not; balance is reported as N/A (not "false") when no matching ran. |
| `de6eae1` | **Phase 4a — LangSmith tracing** | Env-gated (off by default) LLM tracing across the three LLM-calling agents, each call tagged with `task_id`/node/retry-attempt so the self-correcting retry loop is finally inspectable. Off-path forces tracing off to prevent ambient leakage (ADR-003). |
| `7b1e343` | **Phase 4b — MLflow tracking** | Env-gated (off by default) per-analysis run logging — params, metrics, and the SQL/R/narrative as artifacts — via the lightweight `mlflow-skinny` client; best-effort in the worker. Cross-run comparison alongside the `analysis_runs` audit table (ADR-004). |

The ordering tells a story: **get a correct, secure pipeline working first;
then make its transport deployment-agnostic; then add the event layer and harden
both the failure handling and the statistics.** Each step built on a working
base rather than speculating about the future.

---

## 6. Component deep dive

This section walks every meaningful module: *what* it does, *how* it does it,
and *why* it is built that way.

### 6.1 Configuration — `app/core/config.py`

**What.** The single place that reads every external setting (database, Redis,
Anthropic, the R sandbox, Kafka) from environment variables, loaded from `.env`
in local dev.

**Why this matters.** This module is Rule 5 made real. Application code never
contains the string `localhost`; it imports `config.DB_HOST` and friends. The
payoff is concrete: moving to a managed container platform changes environment
values only — no code. Helper functions like `database_url()` and `kafka_client_kwargs()` build
connection details from those values, and `kafka_client_kwargs()` notably emits
*only* the security args that are actually set, so the identical code path serves
a local plaintext broker and a managed SASL/TLS one.

### 6.2 The LLM factory — `app/core/llm.py`

**What.** One cached constructor (`get_llm()`) for the Anthropic chat model.

**How / why.** Two deliberate choices:
- **`temperature=0`.** This platform generates SQL and statistical code. We want
  the *same* question to produce the *same* query — reproducibility, not
  creativity. A non-zero temperature would make runs non-deterministic and
  results non-auditable.
- **A singleton (`lru_cache`).** Every agent shares one identically-configured
  client, so model, token limit, and timeout can never drift between nodes.

It also fails loudly if `ANTHROPIC_API_KEY` is missing, rather than producing a
confusing error deep inside an agent call.

### 6.3 The SQL agent — `app/agents/sql_agent.py`

**What.** Translates the natural-language question into one read-only SELECT,
runs it, saves the result to `data/{task_id}.csv`, and decides (or accepts) the
causal identification.

**The interesting parts:**

- **Two prompt modes.** If the caller already declared the identification
  (`analysis_spec`), the agent only has to write a SELECT projecting exactly
  those columns. If not, the LLM proposes the identification itself, returned in
  the same structured object as the query. Either way the spec ends up explicit
  in the state.
- **Defence in depth against writes.** The query is *executed against a live
  database*, so trusting the prompt is not enough. Three guards stack up:
  1. `_validate_select()` rejects anything that is not a single SELECT/CTE and
     scans for forbidden keywords (`INSERT`, `UPDATE`, `DROP`, …).
  2. The query runs inside a **Postgres `READ ONLY` connection**, so even a
     data-modifying CTE that slipped past the regex is rejected by the database
     itself.
  3. The window filter (event path) is applied by *our* code as a wrapping CTE
     with **bound parameters** — never by string-templating LLM output — so it
     cannot be a SQL-injection vector.
- **Trust the dataframe, not the LLM.** After the query runs, the agent records
  the *actual* dataframe columns and validates the spec against them. If the LLM
  claimed a column that the SELECT did not produce, this fails fast rather than
  letting a phantom column reach the R agent.
- **Empty result is an error.** Zero rows cannot be modelled, so it is treated as
  a failure to retry rather than a "successful" empty run.

### 6.4 The R agent — `app/agents/r_agent.py`

**What.** Given the explicit identification spec, generates a self-contained R
script that estimates a causally adjusted effect.

**Why the spec is explicit.** The agent is told *exactly* which column is the
treatment, which is the outcome, and which are confounders. It does not infer
roles from column names. This is the difference between an auditable
identification strategy and an opaque guess buried inside generated code.

**The estimation contract** (encoded in the system prompt) is precise:
- Read the CSV from the `DATA_FILE_PATH` env var, treating only empty strings as
  missing (so a region coded `"NA"` for North America is not silently dropped as
  a null — a real, subtle bug-class avoided on purpose).
- With confounders present, prefer **propensity-score matching** via `MatchIt`,
  then fit a model on the matched data and report post-match covariate balance.
- Apply a **balance gate** (Section 10): if the match did not actually balance
  the covariates, discard the matched estimate and fall back to covariate
  adjustment on the full data.
- Print exactly one strict JSON line: `p_value`, `ate`, `std_error`, `outcome_sd`,
  `method`, `n_used`, `max_smd`, and `overlap` (propensity common-support, null
  when not matched). The `std_error`/`outcome_sd` feed the E-value (Section 10);
  the sensitivity math itself is computed in Python, never in the LLM-written R.

### 6.5 The executor — `app/agents/executor.py`

**What.** The single bridge between the orchestrator and the R sandbox.

**How / why:**
- It reads the CSV and sends its **content inline (base64) in the HTTP body**,
  not a path. This is the decoupling decision from commit `adbd569`: the worker
  and the sandbox do not share a filesystem when deployed on separate hosts, so
  the data must travel in the request. Because this is plain Python moving bytes,
  Rule 2 (no rows to the LLM) is untouched.
- It uses a **reused `requests.Session`**, so repeated executions get HTTP
  keep-alive instead of a fresh TCP handshake every time.
- **It classifies failures**, and this is the crux of the retry design:
  - Cannot read the CSV, or the sandbox is unreachable / times out / returns 5xx
    → `exec_failed_transient`. Regenerating the R script cannot fix unreachable
    infrastructure, so the *executor* is retried.
  - `Rscript` ran but exited non-zero → `exec_failed_script`. The script is bad,
    so route back to the *R agent*, surfacing stderr so the next attempt can
    correct it.

### 6.6 The R sandbox — `sandbox/main.py` + `sandbox/Dockerfile`

**What.** A deliberately tiny, isolated FastAPI service whose only job is: accept
an R script plus base64 CSV, run it with `Rscript`, return stdout/stderr.

**Why it is its own service.** This is Rule 3. The sandbox **never imports
orchestrator code, never sees the LLM, and holds no database credentials.** A
compromised or runaway script is boxed in.

**Concrete safety measures:**
- **Per-request throwaway directory** (`uuid` named), removed in a `finally`
  block — concurrent executions cannot read or clobber each other's files.
- **Path traversal defence:** the supplied filename is stripped to its basename
  so a name like `../../etc/x` cannot escape the run directory.
- **Size guard:** oversized payloads are rejected *before* decoding, estimating
  the decoded size from the base64 length, so a doomed request never allocates
  the full buffer.
- **Timeout:** `Rscript` runs under a hard wall-clock limit; a script that hangs
  cannot pin a worker forever.
- **Data via env var, not templating:** the script reads `DATA_FILE_PATH` from
  the environment, so untrusted strings are never spliced into R source.

The Dockerfile does one more important thing: it **fails the build if `MatchIt`
does not load.** `install.packages()` only warns on failure, which had let a
broken image (missing the matching library) ship silently; the explicit
`requireNamespace(...) || quit(status=1)` turns that into a hard build error.

### 6.7 The evaluator — `app/agents/evaluator.py`

**What.** Parses the strict JSON the R script printed, normalises it, and applies
the significance rule (`p_value <= 0.05`).

**Robust parsing.** It scans stdout from the **last line backwards** for a JSON
object, so an incidental R warning printed earlier does not break parsing.

**The three-way balance state.** This is a small but careful piece of semantics:
- A matching method ran and balanced well → `balanced = True`.
- A matching method ran and balanced poorly → `balanced = False`.
- No matching ran (e.g. covariate-adjusted `lm`) → `balanced = None` (N/A).

The distinction matters: reporting `False` for a non-matched estimate would
falsely imply it is "poorly balanced," when balance is simply not a meaningful
concept for that method. Reporting N/A is the honest answer.

It also computes the **E-value** (`app/core/sensitivity.py`) from the R-reported
`std_error`/`outcome_sd`, and passes through the propensity `overlap` — so
sensitivity to unobserved confounding is added deterministically here rather than
trusted to the LLM-generated R (Section 10).

### 6.8 The reviewer — `app/agents/reviewer.py`

**What.** Turns the cold numbers into a 2–4 sentence narrative for a
non-technical stakeholder. This is the terminal happy-path node.

**Why it is prompted so carefully.** The system prompt enforces *statistical
humility*:
- It must say plainly when a result is **not** significant rather than
  overstating it.
- It must state that this is an **observational estimate** relying on the
  assumption of no important unobserved confounders — an adjusted association,
  not proof of causation.
- If the method was unadjusted, it must warn that no confounders were controlled
  for.
- If matching was used but balance failed, it must flag the estimate as
  unreliable.

This is what keeps the "friendly explanation" from quietly laundering a shaky
number into a confident business claim.

### 6.9 Persistence — `app/core/persistence.py`

**What.** Writes one row per run to an `analysis_runs` table: the question, the
SQL, the R script, the statistical output, the method, the narrative, status,
and any errors.

**Why.** Celery's result backend is ephemeral; on its own the platform would keep
no durable record of what it computed. For an enterprise analytics tool the audit
trail is the point — every result must be reproducible and traceable back to the
exact query and script that produced it.

**How it stays out of the way.** Persistence is **best-effort**: the worker wraps
`save_run()` in a try/except and swallows failures, because a problem writing the
audit row must never lose the actual result the user is waiting for. The table is
created on demand (idempotent DDL) and updates use an upsert keyed on `task_id`.

### 6.10 The database engine — `app/core/db.py`

**What.** A single cached SQLAlchemy `Engine` for the whole process.

**Why.** An `Engine` owns a connection pool and is expensive to build; creating
one per task would be a serious leak. It is built once (`lru_cache`) with
`pool_pre_ping=True` so a worker that has sat idle (or survived a database
restart) never hands out a dead connection, and `pool_recycle` to dodge
server-side idle timeouts. These settings are tuned for exactly this workload:
long-lived workers that are idle between bursts.

### 6.11 Observability — `app/core/observability.py` (Phase 4)

**What.** One module owns the whole observability story: **LangSmith** traces the
LLM *calls*; **MLflow** records each finished analysis as a comparable
*experiment run*. Both are env-gated and **off by default**.

**Why tracing earns its place.** The orchestrator has a self-correcting retry
loop (Section 7.4): on failure it feeds the last error back into the prompt. Until
Phase 4 that loop was opaque — a failed run left only an `errors` string, with no
way to see whether attempt #2 actually differed from attempt #1 or just burned
another paid call. `run_config()` tags every LLM call with `task_id`, node, and
retry attempt, so the loop becomes searchable in LangSmith. `configure_tracing()`
maps the single gate to LangChain's native `LANGCHAIN_*` env vars, and on the
off-path *forces* `LANGCHAIN_TRACING_V2=false` so an ambient env var cannot
silently start shipping prompts off-box (ADR-003). Raw rows are never traced —
Rule 2 already keeps them out of every prompt.

**Why MLflow, given `analysis_runs` exists.** The audit table is the per-run
**provenance** record; MLflow is the **cross-run comparison** surface (trend the
ATE across runs, compare methods, watch drift as clickstream data arrives). The
field overlap is accepted deliberately, with `analysis_runs` authoritative if the
two ever diverge. `log_causal_run()` logs params (treatment/outcome/confounders/
method), metrics (ate, p_value, max_smd, is_significant, retry_count), and the
SQL/R/narrative as artifacts; failed runs are logged too (status tag), so failures
are visible. It is best-effort in the worker — a tracking failure never loses the
result, the same contract as the audit write. The client is the lightweight
`mlflow-skinny`; the deployment backend is a remote tracking server, with a local
file store as the zero-setup dev default (ADR-004).

---

## 7. The orchestrator: retries, recovery, and fallback

The orchestrator (`app/core/graph.py`) is where the system's resilience lives,
and it is the part that was most deliberately redesigned.

### The happy path

```
sql_agent → r_agent → executor → evaluator → reviewer → END
```

Each node returns a `current_status`. A small **conditional router** function
after each node reads that status and decides where to go next.

### Recovery target depends on the failure, not just the stage

This is the key insight. A failure is not just "something went wrong at stage N";
*what* went wrong dictates *where* recovery should resume:

- **SQL failure** → retry `sql_agent` (regenerate the query).
- **Not a causal question** → the SQL agent (free-text path) sets
  `answerable=false` and the router sends `declined → END` with a helpful message.
  This is the **honesty guard**: an ill-posed or adversarial question is declined
  cleanly, not retried and not forced into a meaningless analysis. A pinned spec
  skips the guard — the caller already declared a valid question.
- **Bad R script** (generation error, non-zero `Rscript` exit, or unparseable
  output) → retry `r_agent` (regenerate the script). Notably, *unparseable output
  caught by the evaluator* routes back to the R agent, because that is a script
  problem, not an evaluation problem.
- **Transient executor/sandbox failure** (unreachable, timeout, 5xx) → retry the
  `executor` itself. Regenerating a perfectly good script cannot fix unreachable
  infrastructure.

### Per-node retry budgets (the redesign in `df2c1fc`)

The original design used **one shared retry counter** for the whole graph. The
flaw: a single hiccup at each of several different stages could exhaust the global
budget even though no individual stage was truly stuck.

The fix: each node has its **own** `MAX_RETRIES` budget, tracked in a
`retries` dictionary keyed by node name. Exhaustion is decided per node. A SQL
agent that has used up its budget does not strand a later, first-time R failure
into the fallback. `retry_count` is kept too, but only as a human-readable "how
hard did this fight" signal for the fallback message and the audit trail.

### Self-correcting retries — `app/agents/feedback.py`

A subtle problem hides here: with a fixed prompt and `temperature=0`, simply
re-running a failed LLM node would reproduce the *identical* failure. So on
retry, the most recent error (truncated to a budget) is fed back into the prompt
with an instruction to diagnose and avoid it. The retry is a genuine second
attempt with new information, not a hopeful repeat.

### The fallback node

When a node exhausts its budget, the graph routes to a terminal `fallback` node
that writes a clear failure narrative and ends. **A task always terminates
cleanly** — it never loops forever and never dies silently.

The entire router layer is built from **pure functions of the state**, which is
why it can be unit-tested exhaustively (`tests/test_graph_routing.py`) with no
LLM, database, or sandbox in the loop.

---

## 8. The event-driven layer

Phase 3 (`df2c1fc`) added a second entry path so the platform reacts to live data
instead of only on-demand requests.

### The shape

```
stream_events.py → producer → Kafka/Redpanda topic → consumer → Postgres
                                                          │
                                            (threshold crossed)
                                                          ▼
                                            enqueue run_causal_analysis
```

A synthetic clickstream of order events is published to a Kafka-compatible topic.
A consumer writes each order into the existing `customers`/`orders` tables and
counts new arrivals. When the count crosses a threshold, it enqueues **the same**
`run_causal_analysis` Celery task the HTTP API uses — the event layer *reuses* the
analysis engine rather than forking it.

### Correctness under failure — the hard part

A streaming consumer that "mostly works" is easy; one that is correct under
crashes and redelivery is not. The consumer (`app/events/consumer.py`) gets four
things right, by design:

1. **At-least-once delivery.** Kafka auto-commit is **off**. The offset is
   committed only *after* the database transaction for that event commits. A
   crash mid-event redelivers it rather than dropping it.
2. **Idempotent inserts.** Inserts use `ON CONFLICT DO NOTHING`, so a redelivered
   event produces no duplicate row.
3. **A durable, restart-safe counter.** The ingest count lives in a Postgres
   `ingest_state` table, incremented only when a row was *actually* inserted
   (detected via `RETURNING`). A redelivered duplicate therefore never
   double-counts, and the count survives a process restart. (An earlier version
   kept the count in memory and lost it on restart — that bug is gone.)
4. **At-most-once triggering per bucket.** The trigger count is advanced inside
   the *same* committed transaction. The only residual risk is a *missed* trigger
   if the process dies in the gap between commit and enqueue — which is the right
   tradeoff: a missed analysis is cheap; a duplicate (LLM + R) run is not.

### Poison pills

A message that can never be valid (wrong shape, missing fields) is detected by
`parse_event()`, which returns `None`. The consumer skips it and commits past it,
rather than retrying it forever and wedging the whole partition. Crucially, a
*transient database* failure is a raised exception handled outside this guard, so
it leaves the offset uncommitted and preserves at-least-once redelivery — the two
failure modes are kept cleanly separate.

### Tumbling windows

Each trigger analyses one fresh, non-overlapping batch of orders: `order_id` in
`(lo, hi]`, where `lo` is the previous trigger's high-water mark. The window is
applied **deterministically by our SQL-wrapping code with bound parameters**, not
by the LLM (which is also why the SQL agent is required to project `order_id`).
The `trigger_due()` decision is a pure function, so the threshold-boundary logic
is unit-tested without Kafka or a database.

---

## 9. Crucial decisions and what drove them

The two formally recorded decisions (ADRs) and the implicit ones all share a
theme: **measure or reason from first principles, then choose — do not default.**

### ADR-001: Event broker → managed Kafka (Upstash), Kafka-compatible locally

The event layer needs a Kafka-compatible broker. The decision was to use a
**managed, serverless Kafka** so application code stays decoupled from broker
infrastructure (the correct pattern for the K8s-bound Phase 5), with local
**Redpanda** speaking the same protocol in dev. Swapping brokers becomes a change
of URL and credentials in `.env`, not a code change. Local Redpanda was rejected
as the *only* broker because it is not reachable outside the Docker network and
adds memory overhead; heavyweight options (Confluent) were over-engineered for
dev/staging.

### ADR-002: Celery broker → RabbitMQ, *not* a metered Redis free tier

This is the decision most worth highlighting, because it was driven by
**measurement, not assumption.**

The free-tier plan assumed Upstash Redis (10,000 commands/day) as the Celery
broker. Before building on that assumption, it was tested empirically: snapshot
Redis command count, run an **idle** worker for 60 seconds, extrapolate.

| Worker config | Commands / 60s idle | Commands / day | vs 10K cap |
|---|---|---|---|
| Default | 91 | ~131,000 | **13× over** |
| Tuned (no gossip/mingle/heartbeat) | 60 | ~86,400 | **8.6× over** |

An idle worker — doing *no work* — would burn the entire daily budget in under
two hours. The cost is **structural**: it is Celery's Redis transport polling the
queue (`BRPOP`), inherent to the design, not a tunable knob (disabling
gossip/mingle/heartbeat removed only ~34%).

The decision: use **RabbitMQ (CloudAMQP free tier)** as the broker. AMQP is
**push-based** — an idle worker holds a connection and waits, so idle traffic is
effectively zero and there is no per-day budget to exhaust. It is also the
canonical, best-supported Celery pairing. Because the broker URL is already
env-driven, this is a config change, not a code change. (Redis can still serve as
the *result backend*, where it is read/written once per task — well within the
cap.)

The lesson recorded here: **a plausible free-tier assumption was wrong by an order
of magnitude, and only a five-minute measurement caught it before it became a
production incident.**

### Implicit decision: inline CSV transport over a shared volume (`adbd569`)

Originally the sandbox read the CSV from a shared Docker volume. That couples the
worker and sandbox to the same filesystem — fine on one host, broken the moment
they are scheduled on different nodes (exactly what K8s does). The transport was
changed so the executor sends the CSV content inline in the HTTP body. The cost
is a size limit and base64 overhead; the benefit is that the sandbox is now
genuinely independently deployable. This is Rule 5 (decoupling) winning over
short-term convenience.

### Implicit decision: explicit identification spec over inferred roles

Letting the R agent guess "treatment vs. outcome vs. confounder" from column
names would be convenient and unauditable. Making `AnalysisSpec` a first-class,
structured, overridable field means the **identification strategy is inspectable
and can be pinned by the caller** — which is what makes a *causal* claim
defensible rather than a correlation dressed up as one.

### ADR-003 & ADR-004: observability is opt-in, and logs to two places on purpose

Phase 4's two decisions share the theme of *deciding consciously rather than
defaulting*. **ADR-003 (tracing egress):** LangSmith is wired in but env-gated
off by default, because enabling it ships schema/SQL/scripts to a hosted store —
a conscious choice that must match the platform's "nothing leaves unless
intended" posture, not an accidental on. **ADR-004 (MLflow vs `analysis_runs`):**
the system logs runs to *both* MLflow and the audit table deliberately — they
answer different questions (cross-run comparison vs. per-run provenance) — using
the lightweight `mlflow-skinny` client with a remote tracking server as the
production backend. Both records also weigh the costs honestly (hosted-store
egress; the `starlette` downgrade `mlflow-skinny` forced).

---

## 10. Statistical correctness: the part most demos skip

A causal platform that returns confident but wrong numbers is worse than no
platform. Several pieces exist purely to prevent that.

### Propensity-score matching with a balance gate (`2ce5ff2`)

For a binary treatment with confounders, the R agent prefers **propensity-score
matching** (`MatchIt`): it pairs treated and untreated units with similar
covariate profiles to approximate a randomized comparison. But matching only
produces a trustworthy estimate **if it actually balanced the covariates.** A
match that leaves the groups dissimilar gives a falsely precise number.

So the script computes the **largest standardised mean difference (SMD)** across
covariates after matching, and applies a **gate**: if `max_smd >= 0.1` (a common
"poor balance" threshold), it **discards the matched estimate** and falls back to
a covariate-adjusted `lm()` on the full data, which does not depend on achieving
match balance. It also falls back on any matching error, and uses an unadjusted
model only when there are genuinely no confounders.

The result: **method selection is data-driven, and an unreliable matched estimate
is never silently reported as the answer.** The chosen `method` and the balance
figure travel all the way to the narrative.

### Honesty propagated to the narrative

As covered in 6.7 and 6.8, the balance state is preserved as a three-way value
(good / poor / N/A), and the reviewer is required to caveat observational
estimates, flag non-significance plainly, and warn when balance failed. The chain
from "the match was poor" to "treat this number with caution" is unbroken.

### A test that would catch a plausible-but-wrong number

The seed data plants a **true** discount effect of ~14 USD on order total. The
deterministic end-to-end test (`tests/test_pipeline_deterministic.py`) runs the
real executor + sandbox + R and asserts the recovered ATE lands near 14 and is
strongly significant — for both the covariate-adjusted and the MatchIt paths. A
silently broken pipeline that returns a wrong-but-plausible number is exactly the
failure this gate is designed to catch.

### Sensitivity to unobserved confounding — the E-value

The whole pipeline only identifies a causal effect *under the assumption of no
unobserved confounding* — which is untestable. Rather than leave that assumption
implicit, every estimate now carries an **E-value** (VanderWeele & Ding):
`app/core/sensitivity.py` converts the effect to a standardised mean difference,
maps it to an approximate risk ratio, and reports how strong an unmeasured
confounder would have to be — associated with **both** treatment and outcome — to
fully explain the result away. The narrative states it in plain words ("an
unmeasured confounder would need a risk ratio of about 3.7 … to overturn this");
the conservative CI-bound E-value is used, so a confidence interval that touches
the null collapses to 1.0 and the result is flagged as not robust. On the matched
path a propensity **overlap** (positivity) fraction is reported too. This is
computed deterministically in Python, never in the LLM-written R.

### Confounded simulation + a placebo make the agent *checkable*

The simulation (`app/sim/effects.py`) is a single source of truth for both the
bulk seed and the live stream. It plants several treatments — a discount (+$14),
a checkout-variant effect (+$6), and a **true-null placebo** (a free-shipping
banner, effect 0) — and, crucially, assigns each treatment with a **confounded**
probability so the naïve difference-in-means is biased. That makes recovery a real
test: a system that just confirms hypotheses fails the placebo, while correct
adjustment recovers ~0. `GET /sim/truth` renders planted-vs-naïve estimates so the
gap is visible.

### Proving the LLM path, not just the plumbing

The deterministic test exercises hand-written R. Two on-demand harnesses exercise
the **model**: `scripts/eval_agent.py` submits free-text questions with no pinned
spec and scores whether the agent self-identifies the right treatment and recovers
the planted effect (including the placebo); `scripts/redteam_agent.py` drives
adversarial questions and checks for leakage. And an **honesty guard** (Section 7)
makes the agent *decline* a question that isn't a well-posed causal one rather than
fabricate a generic analysis — abstention over fabrication.

---

## 11. Security posture

Security here is structural, not bolted on. The full posture (data-boundary
inventory, provider tier, fairness) lives in `docs/SECURITY.md`; the hardening
rationale in `docs/decisions/ADR-005-responsible-ai-hardening.md`. In summary:

- **Untrusted code is boxed *and* contained.** LLM-generated R runs only in the
  sandbox — no DB credentials, no LLM access, no orchestrator code (Rule 3) — now
  also hardened: `cap_drop ALL` (+`NET_ADMIN` only), an `iptables`+`ip6tables`
  egress lockdown (the rows the sandbox holds can't be exfiltrated over the
  network), read-only rootfs with tmpfs-only writable paths, a non-root uid, an
  env allowlist, and a loopback-only port.
- **The database is protected by least privilege + three layers.** The extraction
  query runs as a dedicated read-only role (`causal_ro`, SELECT on the three
  analytics tables only) through `get_readonly_engine`, so a prompt-injected query
  can't read other tables or write at all — on top of keyword validation, a
  `READ ONLY` session, parameterised window filters, and a `statement_timeout`.
- **PII is minimised at the source.** Rule 2 keeps rows out of every prompt; error
  text is redacted *at capture* (so `/status` and the audit DB never see raw
  values); the extracted CSV is purged after each run; `/status` returns a curated
  result, not internal state.
- **Failures fail fast and honestly.** Permanent LLM errors (auth/bad-request,
  classified by HTTP status) skip the retry budget and return an operator-facing
  message; users get actionable per-stage guidance, never a traceback.
- **Secrets live only in the environment** (Rule 5); `.env` is git-ignored with a
  committed `.env.example`.
- **Injection is contained end-to-end, and proven.** A live red-team
  (`scripts/redteam_agent.py`) drives PII-exfiltration, write-escalation, and
  prompt/secret-disclosure questions through the real `/analyze` path — 3/3
  contained, now declined by the honesty guard or failed-gracefully, never leaking
  secrets, the prompt, or rows.
- **New surfaces stay contained.** The Discord bot is a thin API client that
  relays only curated aggregates (Rule 2) and never raw exceptions; the unauth'd
  `/sim` demo routes are env-gated **off by default** (`ENABLE_SIM_ROUTES`).
- **The front door is authenticated and rate-limited** (`app/core/security.py`,
  ADR-007). `/analyze` and `/status` require an `X-API-Key` (matched against
  `INGRESS_API_KEYS`) plus a per-caller fixed-window rate limit, so the real-money
  LLM + R budget can't be spent by anyone who reaches the port. Open-by-default
  (logged warning) keeps local dev/tests green; one env var locks down an exposed
  deployment.

The remaining items are governance, not code: confirming the Anthropic
zero-retention/no-training tier, enabling branch protection so CI gates pre-merge,
and gating the Discord channel / `/sim` for any non-synthetic data — tracked in
`docs/SECURITY.md`.

---

## 12. Testing strategy

The test suite is layered to match where bugs actually live:

- **`tests/test_graph_routing.py`** — the retry/fallback state machine. Pure
  functions, so every transition (happy path, retry-with-budget, exhaustion,
  transient-vs-script branching, per-node independence) is covered with **zero
  infrastructure.**
- **`tests/test_evaluator.py`** — parsing and the three-way balance semantics,
  including the boundary case (`p == 0.05` counts as significant) and the
  N/A-vs-False distinction.
- **`tests/test_event_consumer.py`** — `trigger_due()` boundaries as a pure
  function, plus `ingest_event()` against the **real** Postgres to prove
  idempotent counting and correct, non-overlapping tumbling windows. Poison-pill
  handling is asserted explicitly.
- **`tests/test_pipeline_deterministic.py`** — the golden-path end-to-end gate
  (DB → CSV → sandbox → R → executor → evaluator), with hand-written known-correct
  R for both estimation paths, asserting the true ATE is recovered.
- **`tests/test_ingress_auth.py`** — the front-door guards: API-key auth
  (open/enforced/missing/wrong), the rate-limit budget, and auth-before-limit
  ordering, with the Celery dispatch stubbed so only the ingress is exercised.

The split is intentional: the **logic** (routing, parsing, triggering) is tested
fast and infrastructure-free; the **integration** (real R, real Postgres) is
tested where correctness can only be proven against the real thing. Integration
tests skip cleanly when the stack is not up, so the fast suite always runs.

Supporting operational scripts round this out: `smoke_anthropic.py` (verify the
key/model), `verify_sandbox.py` (verify the R container), and `run_task.py` (drive
the whole graph from the CLI against live services).

---

## 13. Configuration and deployment story

Everything external is an environment variable (`.env.example` is the documented
template). The same image is meant to run in three environments by changing
values only:

- **Local dev:** Postgres + Redis + Redpanda + the R sandbox via
  `docker-compose.yml`; the worker reaches the sandbox at `localhost:8001`,
  Kafka at `localhost:19092`.
- **Inside the compose network:** the worker reaches services by service name
  (`r_sandbox:8001`, `redpanda:29092`) — which is why the broker advertises two
  listeners.
- **Deployment (container PaaS):** managed Postgres, RabbitMQ as the Celery broker
  (ADR-002), managed Kafka with SASL/TLS — all selected by environment, no code
  change.

### Deployment direction (Phase 5): local demo + recorded video; persistent hosting deferred

The original roadmap named Kubernetes + Terraform; that was first dropped in
favour of a container PaaS, and then — given how the project is actually used —
**a persistent deploy was deferred entirely.** The demo is run **locally**
(`docker compose up` + the Discord bot, which connects *outbound* to Discord and
so needs no public host) and **recorded as a video**; see
`docs/DEMO_RUNBOOK.md` for the bring-up, seed, and on-camera script. For an
interview the recording is the artifact and a local screen-share is the live
fallback — both indistinguishable from a hosted run to a viewer, with none of the
idle cost or on-camera failure risk. The Next.js UI from the old roadmap was
superseded by the Discord bot (ADR-006).

The PaaS path remains the documented option **if** a live host is ever needed:
nothing is K8s- or platform-specific (Rule 5 env-config + inline-CSV sandbox
transport), so K8s YAML reduces to a single `fly.toml`/`render.yaml`, an app
Dockerfile (now exists), and a one-shot seed job. Cost analysis: a trimmed stack
(drop Redpanda + the event consumer) runs ~$4.50/mo on a small Hetzner VM (own VM
→ the sandbox keeps its `NET_ADMIN` egress firewall), $0 on Oracle Always Free, or
~$25–35/mo on Fly (PaaS forces the weaker `SANDBOX_NETWORK_ISOLATION=0`); a
4-question demo is ~$0.20 in Anthropic usage. The free-tier tradeoffs
(small-dataset RAM, cold starts, the always-on consumer being hard to keep free)
are weighed in Section 14.

A typical local bring-up:

```bash
docker compose up -d --build
uv run python -m scripts.seed_db          # plant data with a known effect
uv run pytest -v                          # logic + integration tests
uv run python -m scripts.run_task         # drive one analysis end-to-end
```

---

## 14. Honest assessment: limits and open risks

This platform is a well-architected MVP, not a finished product. Stated plainly:

- **The window's correctness rests on an assumption.** Tumbling windows key on a
  monotonically increasing `order_id`. That holds for the synthetic producer; a
  real feed with out-of-order or non-monotonic ids would need windowing on an
  ingestion sequence or timestamp instead. This is documented in the code, but it
  is a real constraint, not a solved problem.
- **The triggered causal question is hardcoded.** The event consumer fires one
  fixed question ("did a discount increase order total?") with a pinned spec.
  That is deliberate for a deterministic demo, but a production system would need
  a way to register which question(s) a stream should answer.
- **Single-consumer assumptions.** The idempotent-counter design is correct for
  the current single-partition / single-consumer setup; scaling to multiple
  partitions/consumers would require revisiting how the durable counter and
  triggering coordinate.
- **Observability is built but unproven at scale (Phase 4).** LangSmith tracing
  and MLflow run-tracking now exist, both off by default. Two honest caveats:
  MLflow's fields overlap the `analysis_runs` table (its only non-redundant value
  is cross-run comparison), and adding `mlflow-skinny` downgraded `starlette`
  1.0.1 → 0.52.1 project-wide — verified harmless for the FastAPI ingress, but a
  dependency cost that landed for a secondary feature.
- **Deployment is decided but not built (Phase 5).** The K8s target was dropped
  for a container PaaS (Section 13). On a *free* tier the platform loses real
  capabilities: only small datasets (R/MatchIt RAM limits), cold starts, and —
  most consequentially — the always-on Kafka consumer is hard to keep running,
  so the Phase 3 event-driven auto-trigger effectively goes dormant unless a
  persistent worker is funded. Collapsing Celery into FastAPI `BackgroundTasks`
  would simplify the deploy but sacrifice crash-durability of in-flight runs.
- **Cost of LLM retries is unbounded in dollars, not in count.** Per-node budgets
  cap the *number* of retries, but each retry is a fresh (paid) LLM call. Under a
  systematically bad prompt this is bounded but not free.
- **The balance threshold (0.1) and significance threshold (0.05) are
  conventions, hardcoded.** They are sensible defaults, but a serious deployment
  would make them configurable per analysis and document the choice.

None of these are hidden; most are called out in code comments. They are the
honest edge of a system that prioritised getting the *architecture* and the
*statistical integrity* right first.

---

## 15. Glossary

- **ATE (Average Treatment Effect).** The estimated average change in the outcome
  caused by the treatment — here, e.g. the dollar uplift in order total from
  receiving a discount.
- **Confounder.** A variable that influences both the treatment and the outcome;
  failing to adjust for it biases the estimated effect.
- **Propensity-score matching.** Pairing treated and untreated units with similar
  covariate profiles to approximate a randomized experiment from observational
  data.
- **SMD (Standardised Mean Difference).** A scale-free measure of how different
  two groups are on a covariate; under ~0.1 after matching is the common
  "well-balanced" rule of thumb.
- **Idempotent.** An operation that can be applied multiple times without changing
  the result beyond the first — here, re-inserting a redelivered event is a no-op.
- **At-least-once delivery.** A messaging guarantee that no event is lost, at the
  cost of possibly redelivering one — which is why idempotency matters.
- **Tumbling window.** Fixed-size, non-overlapping batches of a stream; each event
  belongs to exactly one window.
- **ADR (Architecture Decision Record).** A short document capturing a significant
  decision: its context, the choice, the rejected alternatives, and the reasoning.

---

*This report describes the system through the demo/rigor layer (commit `462620f`):
the confounded multi-treatment simulation, the Discord front door, E-value
sensitivity + positivity, and the honesty guard, on top of the Phase 4
observability and responsible-AI hardening base. For the live diagrams, see
`docs/ARCHITECTURE.md`; for the decision records, see `docs/decisions/` (ADR-001
through ADR-005).*
