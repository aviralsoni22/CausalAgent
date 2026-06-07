"""Fake-storefront routes that drive the demo.

- ``POST /sim/emit`` publishes a batch of synthetic orders onto Kafka (the same
  stream the consumer ingests), optionally focusing one treatment to simulate a
  campaign.
- ``GET /sim/truth`` renders the ground-truth card: the planted ATE vs the naive
  (biased) difference-in-means computed live from the mart. The agent's adjusted
  estimate is what the bot/pipeline produces — this card is what you check it
  against.
- ``GET /sim/`` is a one-file storefront with a button per treatment.

The producer and the next order id are built lazily so importing this module
(and therefore the API app) never requires a live Kafka broker.
"""
from __future__ import annotations

import random
from threading import Lock

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from app.core import config
from app.core.db import get_engine
from app.events.producer import build_producer, publish
from app.events.schemas import OrderEvent
from app.sim import effects

router = APIRouter(prefix="/sim", tags=["simulation"])

# Streamed ids live well above seed data (1..3000) and the CLI streamer
# (100000+); the floor is raised past any existing row on first use so repeated
# emits never silently collide under ON CONFLICT DO NOTHING.
_ID_FLOOR = 200_000
_rng = random.Random()
_lock = Lock()
_producer = None
_next_id: int | None = None


def _ensure_producer():
    global _producer
    if _producer is None:
        _producer = build_producer()
    return _producer


def _reserve_ids(count: int) -> range:
    """Hand out a contiguous, collision-free id block for this batch."""
    global _next_id
    with _lock:
        if _next_id is None:
            with get_engine().connect() as conn:
                current_max = conn.execute(
                    text("SELECT COALESCE(MAX(order_id), 0) FROM orders")
                ).scalar()
            _next_id = max(_ID_FLOOR, int(current_max) + 1)
        start = _next_id
        _next_id += count
    return range(start, start + count)


@router.post("/emit")
def emit(
    count: int = Query(200, ge=1, le=5000),
    treatment: str = Query("baseline"),
) -> dict:
    """Publish ``count`` synthetic orders; ``treatment`` focuses a campaign."""
    focus = treatment if treatment in effects.TRUE_EFFECTS else None
    producer = _ensure_producer()
    ids = _reserve_ids(count)
    for unit_id in ids:
        unit = effects.generate_unit(unit_id, _rng, focus=focus)
        publish(producer, OrderEvent(order_date="2025-01-01", **effects.event_fields(unit)))
    producer.flush()
    return {"emitted": count, "treatment": focus or "baseline", "topic": config.KAFKA_TOPIC}


@router.get("/truth")
def truth() -> dict:
    """Planted ATE vs the live naive (unadjusted, biased) estimate per treatment."""
    rows = []
    with get_engine().connect() as conn:
        for col, planted in effects.TRUE_EFFECTS.items():
            # col is a trusted constant (key of TRUE_EFFECTS), never user input.
            r = conn.execute(
                text(
                    f"SELECT AVG(total_amount) FILTER (WHERE {col} = 1), "
                    f"AVG(total_amount) FILTER (WHERE {col} = 0), "
                    f"COUNT(*) FILTER (WHERE {col} = 1), "
                    f"COUNT(*) FILTER (WHERE {col} = 0) FROM orders"
                )
            ).first()
            treated_avg, control_avg, n_t, n_c = r
            naive = (
                round(float(treated_avg) - float(control_avg), 2)
                if treated_avg is not None and control_avg is not None
                else None
            )
            rows.append(
                {
                    "treatment": col,
                    "confounder": effects.CONFOUNDER[col],
                    "planted_ate": planted,
                    "naive_ate": naive,
                    "n_treated": int(n_t),
                    "n_control": int(n_c),
                    "is_placebo": planted == 0.0,
                }
            )
    return {"truth": rows}


@router.get("/", response_class=HTMLResponse)
def storefront() -> str:
    buttons = "\n".join(
        f'<button onclick="emit(\'{col}\')">Run {col} campaign</button>'
        for col in effects.TRUE_EFFECTS
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>FakeShop — causal demo</title>
<style>
 body{{font-family:system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem}}
 button{{margin:.25rem;padding:.5rem .8rem;cursor:pointer}}
 table{{border-collapse:collapse;margin-top:1rem;width:100%}}
 th,td{{border:1px solid #ccc;padding:.4rem .6rem;text-align:right}}
 th:first-child,td:first-child{{text-align:left}}
 .placebo{{color:#b00}} #log{{color:#555;margin-top:.5rem}}
</style></head><body>
<h1>FakeShop control panel</h1>
<p>Emit synthetic orders onto the live Kafka stream, then check the agent against
the planted truth. Count per batch:
<input id="count" type="number" value="500" min="1" max="5000"></p>
<div>
 <button onclick="emit('baseline')">Emit baseline traffic</button>
 {buttons}
</div>
<button onclick="loadTruth()" style="margin-top:1rem">Refresh ground-truth card</button>
<div id="log"></div>
<table id="truth"><thead><tr><th>Treatment</th><th>Confounder</th>
<th>Planted ATE</th><th>Naive (biased)</th><th>n=1</th><th>n=0</th></tr></thead>
<tbody></tbody></table>
<script>
const cnt = () => document.getElementById('count').value;
async function emit(t) {{
  document.getElementById('log').textContent = 'emitting ' + t + '…';
  const r = await fetch(`/sim/emit?count=${{cnt()}}&treatment=${{t}}`, {{method:'POST'}});
  const j = await r.json();
  document.getElementById('log').textContent =
    `emitted ${{j.emitted}} orders (${{j.treatment}}) → topic ${{j.topic}}`;
}}
async function loadTruth() {{
  const j = await (await fetch('/sim/truth')).json();
  document.querySelector('#truth tbody').innerHTML = j.truth.map(r => `
   <tr class="${{r.is_placebo?'placebo':''}}"><td>${{r.treatment}}${{r.is_placebo?' (placebo)':''}}</td>
   <td>${{r.confounder}}</td><td>${{r.planted_ate}}</td><td>${{r.naive_ate??'—'}}</td>
   <td>${{r.n_treated}}</td><td>${{r.n_control}}</td></tr>`).join('');
}}
</script></body></html>"""
