---
tags: [data-source, etf]
---

# Farside ETF

Daily ETH ETF flows (per issuer + aggregate) scraped from https://farside.co.uk/eth/.

## The TLS fingerprint gotcha

Farside has a **JA3/TLS fingerprint check** that rejects requests from `httpx` with HTTP 403 — but accepts `curl`. So we use a `_curl_get()` subprocess fallback when httpx fails.

`backend/main.py` has the runtime fallback. `scripts/backfill.py` mirrors it for historical backfill. Shared parser lives in `backend/farside_parse.py` (single source of truth).

## Fallback chain

```
farside_csv → farside_html (httpx) → farside_html (curl subprocess) → SoSoValue → stale cache
```

Curl is third in the chain (CSV is preferred when available, httpx is tried even though it fails — for the rare days when Cloudflare loosens the fingerprint check). SoSoValue is a paid-tier ETF data API that requires an API key (best-effort, not always populated).

## Cache TTL

6 hours. ETF flows are reported once daily (T+1 settlement), so polling more often is wasteful.

## See also

- `backend/farside_parse.py` (the parser)
- `_curl_get()` in `backend/main.py` (the curl fallback)
