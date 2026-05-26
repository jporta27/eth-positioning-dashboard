---
tags: [adr, dune, metrics]
status: accepted
date: 2026-05
---

# ADR-002 — Dune partial bucket exclusion

**Status**: Accepted
**Context**: Users reported "los números cambian entre refreshes" — the 1h/6h/24h netflow aggregates were drifting between requests with no real market activity.

## The problem

Dune indexes Ethereum L1 data with ~30–90 min lag. Our query 6984181 ([[Dune (CEX netflows)]]) groups transactions into hourly buckets. The **most recent** bucket Dune returns is being actively indexed: each re-execution adds more transactions to it as indexing catches up.

So consecutive refreshes that hit Dune at, say, T and T+10min, see:
- T:    bucket [22:00–23:00) has N₁ txs counted
- T+10: bucket [22:00–23:00) has N₂ > N₁ txs counted

The 1h aggregate built on this bucket then drifts upward with each refresh, looking like real flow when it's just indexing.

Compound problem on Vercel: multi-Lambda cache state means different requests may hit different in-process caches with different staleness, amplifying the variance.

## The decision

**Detect the in-progress bucket and exclude it from aggregates.** Specifically:

1. Find `max_bucket_ts` = newest hour returned by Dune
2. Compute `bucket_end_ms = max_bucket_ts + 1h`
3. If `(exec_ended_ms − bucket_end_ms) < 90 min` AND we have ≥2 buckets:
   - Treat this bucket as partial
   - Use the **second-most-recent** bucket as the "now" anchor
   - Compute aggregates from `parsed_for_aggregates` (excludes partial)
4. Surface `partialBucketTs` in the response so the frontend can render the bucket on the chart but flag it visually as live/incomplete
5. The `hourly_series` array keeps **all** buckets (including partial) for charting — only aggregates drop it

## Why compare to `exec_ended_ms`, not wall-clock now

By the time we process the result, the cached Dune response may already be 30+ minutes old. Comparing against wall-clock would mark a 60-min-old "partial" bucket as no-longer-partial, even though Dune ran the query when the bucket was still indexing. Anchoring at `exec_ended_at` is correct — it tells us "was this partial when Dune saw it?"

## Cache TTL bump

Also raised from 5 min → 30 min to reduce multi-Lambda variance on Vercel (the lambdas with different cache states would otherwise serve very different values to consecutive page loads). Trade-off: data is up to 30 min older.

## Result

- Refresh stability went from "every refresh shifts numbers" to "stable within 30 min cache window"
- 1h aggregate became the **completed** previous hour, ~1h older than wall-clock but **honest** (no growth-from-indexing)

## See also

- [[Dune (CEX netflows)]]
- [[Z-score (CEX netflows)]]
- [[ADR-003 Direction from z-sign vs noise band]]
