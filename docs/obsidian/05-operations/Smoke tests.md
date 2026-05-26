---
tags: [ops, runbook, tests]
---

# Smoke tests

`scripts/smoke_tests.py` — correctness sanity checks against a running backend. Catches silent value corruption that would poison panels.

## When to run

- Before every push that touches `backend/main.py` or `api/index.py` (per [[ADR-005 Code review standard]] rule 2)
- After redeploying to Vercel (to confirm prod is alive)
- After any env var change

## Usage

```bash
# Local
python scripts/smoke_tests.py

# Against prod
python scripts/smoke_tests.py --host https://eth-positioning-dashboard.vercel.app

# Wait for slow caches to warm before running
python scripts/smoke_tests.py --wait 30
```

Exit code: 0 if all pass, 1 if any fail.

## Checks currently included

| Check | What it asserts |
|---|---|
| `riskfree_rate_in_band` | T-bill rate ∈ (0.001, 0.20) |
| `options_skew_rr25_sign_and_band` | RR25 within plausible range |
| `stables_per_stable_distinct_from_aggregate` | USDT + USDC ≠ aggregate stablecoin total |
| `deribit_basis_monotonic_curve` | Term structure: longer expiry > nearer expiry annualized |
| `etf_flows_total_equals_issuers_sum` | Σ per-issuer = total within tolerance |
| `hl_whales_shape` | `hyperliquidWhales.positions[*]` has mainnetEth + totalSpotEth + hedgeLabel fields + valid enum |

The last one is new (CL-C, follow-up to the Etherscan integration — see [[ADR-005 Code review standard]]).

## Adding a new check

Define a function with the `@check("name")` decorator that returns `(bool_pass, message_str)`:

```python
@check("my_new_check")
def check_my_thing(host):
    d = _get(host, "/api/data")
    if not d: return False, "no response"
    # ... assertion logic
    return True, "passed"
```

Add it to the run sequence in `main()`. New derived metrics need a smoke check OR a unit test (rule 8).

## See also

- [[ADR-005 Code review standard]]
- `backend/tests/test_hedge_label.py` (unit test example)
