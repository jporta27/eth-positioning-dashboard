---
tags: [data-source, dune, critical]
---

# Dune (CEX netflows)

The dashboard's CEX netflow panel runs off Dune query **6984181** — hourly per-CEX inflows/outflows for ETH on Ethereum L1 across 10 major exchanges.

## Endpoint

```
GET https://api.dune.com/api/v1/query/6984181/results?limit=200
Header: X-DUNE-API-KEY: <key>
```

Fetcher: `fetch_dune_cex_netflows` in both [[Two-headed backend|backends]].

## Why limit=200 (pagination)

The unbounded GET returns HTTP 402 ("datapoint cap per request"). With ~1450 rows × 7 cols ≈ 10k datapoints per request, that exceeds the free-plan per-request cap. So we paginate `limit=200` (= 1400 datapoints/req, under the cap) and fan-out the remaining pages in parallel via `/execution/{id}/results?offset=N`.

For 1450 rows: 1 sync call + 7 parallel = ~3s wall clock, fits the [[Deploy targets|Vercel 60s budget]].

## Key rotation

Two keys configured: `DUNE_API_KEY` (primary) and `DUNE_API_KEY_FALLBACK` (secondary account).

`_dune_request` helper tries them in order. On HTTP 402, the offending key is backed-off **per-method** for 6h, then the next key is tried.

**Per-method matters**: primary may 402 on `POST /execute` (write quota) while still serving `GET /results` (already-cached read). Backing off the whole key would force unnecessary fallback traffic.

## Limitations

- Free plan = 2,500 credits/month
- **Billing cycle**: 11th of each month → 11th of next (NOT calendar month). Important when planning runway.
- See [[Dune API quota]] runbook for what to do when exhausted.

## Partial-bucket exclusion (critical)

The most recent hour bucket Dune returns is **still being indexed** (~30–90 min lag). Including it in aggregates makes consecutive refreshes show different numbers for the same "1h" window — that was the original `data inconsistente entre refreshes` bug.

See [[ADR-002 Dune partial bucket exclusion]] for the algorithm and why we anchor at `exec_ended_at` (not wall-clock now).

## Public V2 only

The fallback key (different account) can read any **public** Dune V2 query. It cannot:
- Read **private** queries (404 not 402)
- Trigger executions on V1/Spark-engine queries (400 "Deprecated query engine")

So **query 6984181 must remain public** for the fallback to actually help. Locked-private = single-point-of-failure for the netflows panel.

## Cache TTL

30 minutes (raised from 5 min). Trade-off: less variance across multi-Lambda invocations on Vercel, less credit burn, but slightly older data per refresh. See [[ADR-002 Dune partial bucket exclusion]].

## See also

- [[Z-score (CEX netflows)]]
- [[Dune API quota]] (runbook)
- [[ADR-002 Dune partial bucket exclusion]]
- [[ADR-003 Direction from z-sign vs noise band]]
