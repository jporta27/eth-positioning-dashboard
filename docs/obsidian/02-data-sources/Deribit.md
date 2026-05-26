---
tags: [data-source, deribit, options]
---

# Deribit

ETH options chain: IV per strike, OI per strike, futures basis per expiry.

Endpoint: `/api/v2/public/get_book_summary_by_currency`
- `kind=option` → options data (IV, OI, mark prices)
- `kind=future` → dated futures basis

Used for:
- Options skew (RR25, BF25, max pain, gamma flip)
- Term-structure IV curve (DTE 0–180 days)
- Spot-perp basis (derived)

## Instrument naming

Format: `ETH-DDMMMYY-STRIKE-C|P` (e.g. `ETH-26DEC25-3000-C`).

Parser: `_parse_deribit_option_instrument` in `backend/main.py`. Assumes 2-digit year decodes as `2000 + int(yr)` — works through 2099.

## See also

- [[Macro (Yahoo + FRED)]] (risk-free rate for BS delta interpolation)
