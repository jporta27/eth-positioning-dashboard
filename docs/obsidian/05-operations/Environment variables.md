---
tags: [ops, runbook]
---

# Environment variables

## Local (`backend/.env`)

The file `backend/.env` is **gitignored**. Contents:

```
DUNE_API_KEY=<primary key>
DUNE_API_KEY_FALLBACK=<secondary account key>
DUNE_QUERY_ID=6984181
ETHERSCAN_API_KEY=<v2 key>
HYPERLIQUID_WHALE_ADDRESSES=0xabc,0xdef,...    # optional, comma-separated
```

`backend/.env` was accidentally tracked historically — the legacy `DUNE_API_KEY` is in git history at commit `9b8b119`. **Never `git add -A` near `backend/.env`** — use specific paths.

## Vercel (Project Settings → Environment Variables)

Must mirror the local `.env` (minus things that don't apply to serverless):

| Variable | Required for | Default if missing |
|---|---|---|
| `DUNE_API_KEY` | CEX netflows panel | `cexNetflows = {}` |
| `DUNE_API_KEY_FALLBACK` | Rotation on primary 402 | Single-key mode |
| `DUNE_QUERY_ID` | Override default query 6984181 | uses `6984181` |
| `ETHERSCAN_API_KEY` | Mainnet ETH balance in [[Hedge ratio + label]] | `mainnet_eth = 0.0` everywhere |
| `HYPERLIQUID_WHALE_ADDRESSES` | Override seed whale list | uses 5 hardcoded defaults |

### Vercel env var gotchas

1. **Redeploy required**: env vars do NOT inject into existing deployments. After adding/changing a var you MUST trigger a redeploy.
2. **Scope**: tick **Production, Preview, Development** (all 3) unless you have a reason not to.
3. **Cache**: Lambda may serve cached responses up to TTL after redeploy. Wait 5–30 min (depends on which cache) for fresh data.

See [[Vercel deployment]] for the redeploy procedure.

## Railway

Same as Vercel — set the same vars in Railway dashboard. Long-running process so changes propagate on container restart.

## See also

- [[Vercel deployment]]
- [[Dune API quota]]
