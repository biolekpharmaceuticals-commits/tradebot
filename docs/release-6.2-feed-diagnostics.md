# Release 6.2 — NIFTY Futures Feed Diagnostics

Release 6.2 is a diagnostics-only extension of the Release 6.1 paper scalp runtime.

## Purpose

Release 6.1 measured 392 out-of-order NIFTY futures ticks out of 3,650 execution/futures ticks during the observed session (10.74%). Release 6.2 instruments that behavior before any paper execution is enabled.

## Safety posture

- Live order transport remains unavailable.
- Existing stale/future and out-of-order rejection rules are unchanged.
- Equal exchange timestamps remain accepted, matching the Release 6.1 engine behavior.
- The diagnostics do not modify signals, fills, risk limits, order types, or broker routing.
- Operators should keep paper execution disabled and the kill switch active while diagnosing the feed.

## Added execution-feed telemetry

`scalp_latency.json` and `run_scalp_status.py` now expose:

- execution callback count
- accepted futures tick count and acceptance percentage
- out-of-order rejection count and percentage
- stale/future rejection count and percentage
- equal exchange timestamp count
- advancing exchange timestamp count
- callback inter-arrival p50/p95/p99/max
- accepted exchange timestamp-step p50/p95/p99/max
- maximum reorder depth in milliseconds
- current and maximum consecutive reorder burst
- quote-change counts on reordered ticks for last price, bid, and ask
- up to 25 recent reorder samples, including callback sequence, raw exchange timestamp, previous accepted timestamp, exact reorder delta, quote fields, and burst depth

## Runtime verification

After deployment and market ticks have accumulated:

```bash
/opt/tradebot/venv/bin/python /opt/tradebot/app/run_scalp_status.py \
  --config /etc/tradebot/config.yaml \
  --events 10
```

Inspect the top-level `execution_feed_diagnostics` object. The same data is also persisted under `latency.execution_feed_diagnostics` in `/opt/tradebot/logs/scalp_latency.json`.
