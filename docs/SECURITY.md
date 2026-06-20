# CausalAgent — Security & Responsible-AI Posture

How this system defends the four surfaces of an LLM application — **input,
action, output, data** — and the governance decisions that aren't code. Security
here is structural (designed in), not bolted on. See `ARCHITECTURE.md` for the
system shape and `decisions/ADR-005-responsible-ai-hardening.md` for the rationale
behind the controls summarised here.

> The governing assumption: every input to the model is untrusted, and every
> output is unverified until checked. The architecture is built so that even a
> fully hijacked LLM has a small blast radius.

---

## 1. Data boundary — what crosses to the model provider

**Data minimisation is the master control.** Rule 2 (no raw rows to the LLM) means
the sensitive asset — customer rows — *never enters a prompt*. What actually
crosses the perimeter to Anthropic is narrow and inventoried:

| Agent | Sent to the model | Never sent |
|---|---|---|
| `sql_agent` | DB **schema** (column names/types), the user's NL question, redacted prior-error text | Any row values, credentials |
| `r_agent` | NL question, the identification **spec** (column names), the column list, redacted error | Any row values |
| `reviewer` | NL question, the deterministic **interpretation** line, the **statistical outputs** (ATE, p-value, method, n, SMD, E-value, overlap) | Any row values |

Never crosses the boundary, by construction: **raw customer rows, PII values, DB
credentials, API keys.** The rows flow only DB→CSV→sandbox (plain Python), and the
CSV is deleted after the run (§3).

### Provider-tier requirement (decision)
Because what crosses is schema + stats + the NL question (not rows), residual
sensitivity is low — but it is still third-party egress and must be a conscious
choice:

- **Use a zero-retention / no-training Anthropic tier** for any non-toy
  deployment, and sign the DPA. Confirm region against data-residency obligations
  *before* shipping.
- The same posture governs the two **opt-in** observability sinks (LangSmith,
  MLflow): both default OFF and, when on, ship schema/SQL/scripts/stats — never
  rows — to a hosted store. See ADR-003 / ADR-004.

### Front door — Discord egress (decision)
The Discord adapter (`app/bots/`) is a thin client over the HTTP ingress — it
never imports the worker and only relays the **curated result** (narrative,
interpretation, stats) back to a channel. By Rule 2 + the curated `public_result`,
only aggregates/schema cross to Discord — **no rows, no generated SQL/R, no
secrets**. But Discord is still a third-party egress boundary, so for real data:
the **channel's access control is the data boundary** (who can read the channel),
and the bot token lives only in env (`DISCORD_BOT_TOKEN`), never in code or logs.
The bot surfaces no raw exception text to users (transport errors are logged
server-side; the user sees a generic message).

---

## 2. Input surface — prompt injection

The user's NL question (and retried error text) is untrusted and could carry
injected instructions. Defence in depth, weakest→strongest:

1. **Structural separation.** The question goes in the *human* role; system rules
   stay in the system role. Untrusted text is never interpolated into the system
   prompt.
2. **Labelled as data.** Each agent's system prompt instructs it to treat the
   question as a description of *what to analyse* and to ignore embedded
   instructions. (Reduces *incidence* — not a boundary on its own.)
3. **The boundary that actually contains damage** is least privilege on the action
   surface (§4): a hijacked SQL agent still hits a read-only role and a validator.

---

## 3. Data surface — PII handling

- **Rule 2:** rows never enter an LLM call (the core invariant).
- **Redaction at capture.** `feedback.record_failure` sanitises error text
  (emails, long numeric runs) *at the point it's recorded*, so every downstream
  sink — the retry prompt, the `/status` API, the `analysis_runs` audit table —
  only ever sees redacted text. (Redacting at one sink and forgetting another is
  the bug this design avoids.)
- **Extracted CSV is purged** after every run, success or failure
  (`cleanup.purge_extracted_data`, called in a `finally`) — customer rows don't
  linger at rest on the orchestrator.
- **Logs don't echo values.** Poison-pill ingest logs field locations + error
  types, never the offending PII payload.
- **`/status` returns a curated result** — narrative, interpretation, stats,
  status — not the full internal state (no `r_script`, `sql_query`, paths). Full
  provenance stays server-side in `analysis_runs`.

---

## 4. Action surface — least privilege

The two things the model can *cause* are a SQL read and R execution. Both are
boxed so a hijack can't escalate:

- **SQL (read) — three independent layers.** (1) `_validate_select` rejects
  anything but a single read-only SELECT/CTE; (2) the agent connects as a
  **least-privilege role** (`causal_ro`) granted SELECT on *only* the three
  analytics tables — no other tables, no writes, no superuser functions; (3) the
  session is additionally `READ ONLY`. A `statement_timeout` bounds a runaway
  query. Window filters use bound params, never string-built from model output.
- **R (execute) — isolated sandbox.** No DB credentials, no LLM access, no
  orchestrator code. Hardened: `cap_drop ALL` (re-adding only `NET_ADMIN` to
  install the firewall), **egress lockdown** (`iptables` + `ip6tables` OUTPUT
  policy DROP — a script cannot exfiltrate over the network), read-only rootfs
  with tmpfs-only writable paths, non-root uid, an env allowlist (R sees only its
  toolchain vars, not container secrets), per-request throwaway dirs, basename
  stripping against path traversal, size + time guards, and a loopback-only port.
- **Ingress access control — closes the prior open finding.** The HTTP front door
  (`/analyze`, `/status`) requires an `X-API-Key` matched against `INGRESS_API_KEYS`
  and applies a per-caller fixed-window rate limit (`app/core/security.py`),
  bounding who can spend the real-money LLM + R budget. It is **open by default**
  (empty key set → a logged startup warning) so local dev, the test suite, and the
  smoke path stay green; any exposed deployment locks down via one env var. The
  limiter is in-process/per-worker (Redis-backed noted-not-built for multi-replica);
  the `/sim` storefront gets rate-limiting only (keyless browser use) and `/health`
  stays open for probes. Set `INGRESS_API_KEYS` **before** exposing the service —
  in open mode the limiter keys on the unvalidated header, so auth is the real gate.

No high-impact irreversible actions exist (no send/pay/delete/external write), so
no human-confirmation gate is required today. The event-driven auto-trigger fires
a read-only advisory analysis — low blast radius — and is a documented, accepted
autonomous path.

- **Demo `/sim` routes are off by default.** The fake-storefront routes
  (`app/sim/routes.py`: `POST /sim/emit`, `GET /sim/truth`, `GET /sim/`) are
  unauthenticated and state-changing (publish synthetic events, grow the mart), so
  they are mounted only when `ENABLE_SIM_ROUTES` is explicitly set — secure by
  default, enabled by the local compose stack. They emit only synthetic data and
  must never be exposed in a deployment handling real data.

---

## 5. Output surface & honesty

- **The result is grounded and honest.** The reviewer narrates only the supplied
  statistics; the prompt mandates the causal caveat ("observational estimate, not
  proof of causation"), warns on imbalance, and abstains on non-significance.
- **Interpretation is surfaced for verification.** A deterministic line —
  *"Measured the effect of X on Y, adjusting for Z, over N observations"* — lets
  the user confirm we answered the question they meant, not a misread one.
- **Honesty guard — abstain over fabricate.** On the free-text path, a question
  that is not a well-posed causal question over the schema (no identifiable
  binary treatment + numeric outcome, unrelated, or an instruction to do something
  else) is **declined cleanly** with a helpful message, instead of being forced
  into a meaningless analysis. The SQL agent flags `answerable=false`; the graph
  routes `declined → END` (not a failure/retry). A pinned spec never declines.
- **Sensitivity to unobserved confounding.** Every estimate carries an **E-value**
  (`app/core/sensitivity.py`, computed deterministically in Python — not in the
  LLM-written R) and, on the matched path, a propensity **overlap** (positivity)
  fraction. The narrative states how strong an unmeasured confounder would have to
  be to overturn the result — making the core untestable assumption explicit
  rather than hidden.
- **Failure is honest and actionable**, mapped to a next step; a permanent
  model/config error (auth/bad-request) fails fast with an operator-facing message
  rather than burning the retry budget or fabricating a result.
- **No HTML sink today** (JSON API, no frontend). Any future UI rendering the
  narrative/interpretation must escape it — it is model-influenced text.

---

## 6. Fairness

`age` and `region` are used as **confounders to adjust for** — legitimate
statistical control to de-bias a causal estimate — **not as features that decide
about a person.** The output is a population-level average treatment effect, not a
per-customer score. **The tool must not be repurposed to make individual-level
decisions on protected or proxy attributes.** If per-cohort effect estimates are
ever added, compare error rates across cohorts, not just the aggregate.

---

## 7. Evaluation & gating

- **Regression tests** cover the security-critical logic: SQL validation
  (injection/read-only), redaction-at-capture, CSV cleanup, error
  classification/fail-fast, ingress auth + rate limiting
  (`tests/test_ingress_auth.py`), interpretation, fallback, and progress.
- **Live injection red-team** (`scripts/redteam_agent.py`) drives adversarial
  questions (PII exfiltration, write/forbidden-table escalation, prompt/secret
  disclosure) through the real `/analyze` path and checks the result for leaked
  secrets, verbatim prompt disclosure, and row dumps — **3/3 contained** (now
  declined or failed-gracefully, never leaking). Closes the prior open item.
- **Agent identification eval** (`scripts/eval_agent.py`) submits free-text
  questions with no pinned spec and scores recovery + confounder coverage against
  planted ground truth (incl. a true-null placebo) — proving the LLM path, not
  just the deterministic plumbing.
- **CI gate** (`.github/workflows/ci.yml`) runs the suite (with a real Postgres)
  on push + PR. The two harnesses above are live-LLM tools, run on demand.

---

## Open items (decisions / follow-ups, not code)

- [ ] **Confirm the Anthropic provider tier** (zero-retention / no-training) and
      sign the DPA before any deployment handling real customer data.
- [ ] **Enable branch protection** on `main` requiring the CI check, and route
      changes via PRs — otherwise CI reports failures *after* a bad commit is
      already on main (a smoke alarm, not a gate).
- [x] **Add a live-LLM injection red-team eval** — done: `scripts/redteam_agent.py`
      (3/3 contained) plus the honesty guard that declines adversarial questions.
- [x] **Authenticate + rate-limit the HTTP ingress** — done (`app/core/security.py`,
      ADR-007): `X-API-Key` against `INGRESS_API_KEYS` + per-caller rate limit on
      `/analyze`/`/status`; open-by-default for local, locked down via one env var.
- [ ] **Gate the Discord channel / `/sim` for real data** — for any non-synthetic
      deployment, restrict who can read the bot's channel (the egress boundary) and
      keep `ENABLE_SIM_ROUTES` off.
- [ ] **Consider a confirm-the-interpretation step** before the run (human-in-the-
      loop) — currently the analysis auto-completes; acceptable for advisory
      analytics, worth revisiting if outputs ever drive consequential decisions.
