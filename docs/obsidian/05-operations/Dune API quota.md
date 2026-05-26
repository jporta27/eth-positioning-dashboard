---
tags: [ops, runbook, dune]
---

# Dune API quota

What to do when Dune starts returning HTTP 402 ("This api request would exceed your configured datapoint limit per billing cycle").

## Billing cycle math

- **Free plan**: 2,500 credits/month
- **Cycle**: starts the day of the month you signed up. Example: signed up Apr 11 → cycle runs **11th-of-month to 11th-of-next** (NOT calendar month). Important when planning runway.

## Current burn rate

With cache TTL = 30 min and partial-bucket exclusion, we burn approximately:
- ~25 credits/day
- ~750 credits/month
- Runway: ~3.3x the cycle quota → plenty of headroom

If you see 402 in the logs, something is wrong (cache miss explosion, traffic spike, regression).

## Diagnosis

When `cexNetflows = {}` appears in the response:

```bash
# Local Etherscan-direct check (replace with your key)
curl -H "X-DUNE-API-KEY: <key>" "https://api.dune.com/api/v1/query/6984181/results?limit=10"
```

Possible results:

| Response | Cause |
|---|---|
| 200 with data | Cache stuck stale, restart backend |
| 402 quota exceeded | Quota actually exhausted, wait or upgrade |
| 404 not found | Query is private and key isn't owner |
| 400 deprecated engine | Query is on V1/Spark, must migrate to V2 (see [[ADR-006 Removed backtest framework]] for context) |

## Recovery options when quota exhausted

1. **Wait** until next cycle reset (see math above)
2. **Upgrade** Dune plan ($) for more credits
3. **Rotate to fallback key** — already automatic via `_dune_request`. Requires `DUNE_API_KEY_FALLBACK` set. Fallback can only:
   - Read public V2 queries
   - Trigger executions on queries owned by that account OR public V2 queries
4. **Make the query public** — single click in Dune UI; lets fallback read it
5. **Fork the query to a new account** — query 6984181 is V1/Spark, deprecated. A fresh query in V2 (Dune SQL) is required if you want the fallback to also trigger executions.

## See also

- [[Dune (CEX netflows)]]
- [[Environment variables]]
- [[ADR-002 Dune partial bucket exclusion]]
