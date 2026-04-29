# Reports summary — POST PROMPT-A fixes + extended sample

Generated automatically by `scripts/build_reports_summary.py` from
`reports/*.json`. Compares against `reports/_archive_pre_purge/`.

Setup applied to every report in this batch:
- purge_overlapping_events=True (FIX 1)
- sharpe_per_event + sharpe_annualized_by_event_freq (FIX 2)
- DSR uses sharpe_per_event with n_obs=n_valid (FIX 2)
- n_trials=10 (auto-tracked, FIX 3 — the full search width)
- Klines+funding extended to 5y (FASE B1)
- Farside ETF unblocked (FASE B2)

## Aggregate event study at 4h horizon — new runs

| signal | n_events | n_purged | n_valid | 4h mean | 4h CI95 | IC | p | hit | base | sharpe_evf | DSR | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `funding_hot_inflow` | 175 | 141 | 34 | 0.0011 | [-0.0023, 0.0046] | 0.015 | 0.933 | 0.559 | 0.507 | +0.30 | 0.000 | **no_edge** |
| `funding_hot_outflow` | 286 | 231 | 55 | -0.0003 | [-0.0070, 0.0058] | 0.117 | 0.382 | 0.509 | 0.507 | -0.03 | 0.000 | **no_edge** |
| `funding_hot_abs` | 459 | 395 | 64 | -0.0012 | [-0.0070, 0.0035] | 0.011 | 0.929 | 0.469 | 0.507 | -0.20 | 0.000 | **no_edge** |
| `funding_extreme_inflow` | 94 | 74 | 20 | 0.0016 | [-0.0025, 0.0056] | 0.322 | 0.185 | 0.700 | 0.507 | +0.34 | 0.000 | **insufficient** |
| `funding_extreme_outflow` | 168 | 116 | 52 | 0.0017 | [-0.0052, 0.0079] | -0.154 | 0.265 | 0.558 | 0.507 | +0.24 | 0.000 | **no_edge** |
| `funding_extreme_abs` | 262 | 198 | 64 | 0.0016 | [-0.0041, 0.0063] | 0.106 | 0.410 | 0.625 | 0.507 | +0.28 | 0.000 | **no_edge** |
| `stables_expanding` | 68 | 56 | 7 | - | - | - | - | - | - | - | - | **insufficient** |
| `stables_contracting` | 77 | 66 | 9 | - | - | - | - | - | - | - | - | **insufficient** |
| `etf_buying` | 14 | 0 | 14 | - | - | - | - | - | - | - | - | **insufficient** |
| `etf_selling` | 19 | 0 | 19 | - | - | - | - | - | - | - | - | **insufficient** |

## Same row from the archived (pre-purge) runs

| signal | n_obs (old) | n_valid (old) | 4h mean (old) | CI95 (old) | IC (old) | p (old) | sharpe_ann (old) | DSR (old) | verdict (old) |
|---|---|---|---|---|---|---|---|---|---|
| `funding_hot_inflow` | 77 | 77 | 0.0004 | [-0.0024, 0.0033] | -0.056 | 0.630 | +1.47 | 1.000 | no_edge |
| `funding_hot_outflow` | 138 | 138 | -0.0001 | [-0.0030, 0.0025] | -0.043 | 0.612 | -0.38 | 0.000 | no_edge |
| `funding_hot_abs` | 213 | 213 | 0.0001 | [-0.0020, 0.0021] | -0.001 | 0.989 | +0.22 | 0.640 | no_edge |
| `funding_extreme_inflow` | (no archive — signal newly unblocked or freshly added) | - | - | - | - | - | - | - | - |
| `funding_extreme_outflow` | (no archive — signal newly unblocked or freshly added) | - | - | - | - | - | - | - | - |
| `funding_extreme_abs` | (no archive — signal newly unblocked or freshly added) | - | - | - | - | - | - | - | - |
| `stables_expanding` | 68 | 13 | 0.0059 | [0.0012, 0.0104] | -0.214 | 0.472 | +31.36 | 1.000 | insufficient |
| `stables_contracting` | 77 | 22 | 0.0026 | [-0.0031, 0.0081] | -0.160 | 0.482 | +8.90 | 1.000 | insufficient |
| `etf_buying` | (no archive — signal newly unblocked or freshly added) | - | - | - | - | - | - | - | - |
| `etf_selling` | (no archive — signal newly unblocked or freshly added) | - | - | - | - | - | - | - | - |

## How the numbers moved

- **Sharpe values are not directly comparable.** Old reports used
  `HORIZON_PERIODS_PER_YEAR` annualization (8760 for 1h, 365 for 1d),
  which assumes the strategy could re-fire every horizon period. New
  reports use `events_per_year` annualization, which is operationally
  honest. For a signal with 77 events in 2y, the new Sharpe is the
  old × sqrt(77/8760) ≈ 0.094× — a ~10× compression that brings them
  into the realm of reasonable strategy Sharpes (0–3) instead of
  fantasy values like ±17.
- **DSR moves from saturated to discriminating.** The previous code
  fed the annualized Sharpe into the deflated-Sharpe formula whose
  variance term assumes per-period Sharpe; that mismatch pinned DSR
  at 1.0 on most reports regardless of true edge. With the per-event
  Sharpe input + n_trials=10 (vs the old 1), DSR now actually
  separates real edges from search noise.
- **n_events shrinks where purge bites.** Funding signals fire often
  enough that the 7d-horizon purge drops a meaningful fraction;
  stables fire ~monthly so the purge is mostly a no-op. The new
  `n_observations` in the JSON reflects pre-purge count; `n_valid`
  in the per-horizon block reflects post-purge usable events.

## Verdict legend

- **edge**: CI95 excludes 0 AND |IC|>0.05 AND p<0.05 AND DSR>0.7 — actionable.
- **weak**: CI95 excludes 0 but at least one of {IC, p, DSR} is below threshold.
- **no_edge**: CI95 spans 0 — can't reject no-effect at 95%.
- **insufficient**: n_valid < 30 — sample too small for stable stats.
