"""Smoke tests for the Fase 1 endpoints — correctness sanity checks.

Purpose: catch silent value corruption that would poison backtests. These are
cheap bound checks, not exhaustive — they're meant to run after every backend
deploy and in CI against a running server.

Usage:
    # Run against localhost:8000 (default)
    python scripts/smoke_tests.py

    # Against a specific host
    python scripts/smoke_tests.py --host https://eth-positioning-dashboard.vercel.app

Exit code: 0 if all checks pass, 1 if any fails. Prints a per-check summary.
"""

import argparse
import sys
import time
import urllib.request
import urllib.error
import json
from typing import Optional


# ── Helpers ──────────────────────────────────────────────────────────
def _get(host: str, path: str, timeout: int = 30) -> Optional[dict]:
    """Return parsed JSON from GET host+path, or None on failure."""
    url = host.rstrip("/") + path
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "smoke-tests/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} at {url}")
        return None
    except Exception as e:
        print(f"  network error at {url}: {e}")
        return None


results: list = []  # list of (name, ok, msg)


def check(name: str):
    """Decorator to register a check. Pass/fail determined by return value:
       truthy = pass, falsy with message = fail, exception = fail."""
    def decorator(fn):
        def wrapper(host):
            print(f"[{name}] running ...")
            try:
                ok, msg = fn(host)
                status = "PASS" if ok else "FAIL"
                print(f"[{name}] {status}: {msg}")
                results.append((name, ok, msg))
            except Exception as e:
                print(f"[{name}] ERROR: {type(e).__name__}: {e}")
                results.append((name, False, f"{type(e).__name__}: {e}"))
        return wrapper
    return decorator


# ── Checks ───────────────────────────────────────────────────────────
@check("riskfree_rate_in_band")
def check_riskfree(host):
    """rate in (0.001, 0.20) — 3M T-bill never went > 20% or < 0.1% in the
    post-ZIRP era. Values outside are provider-convention corruption."""
    d = _get(host, "/api/riskfree")
    if not d:
        return False, "no response"
    if d.get("status") == "warming":
        return False, "still warming"
    rate = d.get("rate")
    if rate is None:
        return False, "missing rate field"
    if not (0.001 < rate < 0.20):
        return False, f"rate={rate} out of band (0.001, 0.20); source={d.get('source')} series={d.get('series')}"
    return True, f"rate={rate*100:.3f}% source={d.get('source')}/{d.get('series')}"


@check("options_skew_rr25_sign_and_band")
def check_skew(host):
    """RR25 at 30d canonical should be in (-0.15, +0.15). For ETH,
    RR25 ≥ 0 in most regimes (calls premium or parity); a heavily negative
    RR25 (> -0.03 say) is rare and usually signals stress. We bound both
    tails and flag if the magnitude is unreasonable."""
    d = _get(host, "/api/options/skew")
    if not d:
        return False, "no response"
    if d.get("status") == "warming":
        return False, "still warming"
    canon = d.get("canonical", {}) or {}
    t30 = canon.get("t30d")
    if not t30:
        return False, f"canonical t30d missing; available: {list(canon.keys())}"
    rr25 = t30.get("rr25")
    if rr25 is None:
        return False, "t30d.rr25 is None"
    if not (-0.15 < rr25 < 0.15):
        return False, f"RR25 at 30d = {rr25} out of (-0.15, +0.15); likely corrupt"
    # Also sanity check IVs are in a plausible band
    iv_atm = t30.get("ivAtm")
    if iv_atm is None or not (0.10 < iv_atm < 2.0):
        return False, f"ivAtm = {iv_atm} out of (0.10, 2.0); decimal-convention bug?"
    r = d.get("riskFreeRate")
    if r is None or not (0.001 < r < 0.20):
        return False, f"riskFreeRate embedded in skew = {r} out of band"
    return True, f"t30d rr25={rr25:+.4f} bf25={t30.get('bf25'):+.4f} ivAtm={iv_atm:.3f} r={r:.4f}"


@check("stables_per_stable_distinct_from_aggregate")
def check_stables(host):
    """aggregate.delta7dPct must be non-None AND the per-stable deltas
    must differ from the aggregate by a real amount (otherwise the per-stable
    endpoint is returning the aggregate ignoring the stablecoin param).
    Also the sum of USDT+USDC (currentUsd) should be 75-95% of aggregate —
    sanity check for coverage."""
    d = _get(host, "/api/stables/supply")
    if not d:
        return False, "no response"
    if d.get("status") == "warming":
        return False, "still warming"
    agg = d.get("aggregate", {})
    if agg.get("delta7dPct") is None:
        return False, "aggregate.delta7dPct is None"
    by = d.get("byStablecoin", {})
    if not by:
        return False, "byStablecoin empty"
    # Distinctness
    bad = []
    for sym, v in by.items():
        if v.get("note"):
            bad.append(f"{sym}:{v['note']}")
            continue
        if v.get("delta7dPct") is None:
            bad.append(f"{sym}: delta7dPct None")
            continue
        # Endpoint regression would leave per-stable numbers == aggregate exactly
        if abs(v["delta7dPct"] - agg["delta7dPct"]) < 1e-6:
            bad.append(f"{sym}: delta7dPct identical to aggregate ({v['delta7dPct']})")
    if bad:
        return False, "; ".join(bad)
    # Coverage
    agg_usd = agg.get("currentUsd") or 0
    tracked_usd = sum((v.get("currentUsd") or 0) for v in by.values())
    if agg_usd == 0:
        return False, "aggregate.currentUsd is 0"
    coverage = 100 * tracked_usd / agg_usd
    if not (60 <= coverage <= 100):
        return False, f"coverage {coverage:.1f}% of aggregate is outside (60-100%)"
    dirs = [v["delta7dPct"] > 0 for v in by.values() if v.get("delta7dPct") is not None]
    agg_pos = agg["delta7dPct"] > 0
    same_dir = sum(1 for d_ in dirs if d_ == agg_pos)
    return True, (f"agg7d={agg['delta7dPct']:+.3f}% "
                  f"per-stable: {[(s, v['delta7dPct']) for s, v in by.items() if v.get('delta7dPct') is not None]} "
                  f"coverage={coverage:.1f}% same-dir={same_dir}/{len(dirs)}")


@check("deribit_basis_monotonic_curve")
def check_basis_deribit(host):
    """The dated-futures basis curve shouldn't have wild oscillations between
    adjacent expiries. We allow up to 5pp of annualized-basis delta between
    consecutive expiries; more than that suggests a quote outlier or bad parse.

    Filter: DTE >= 3 AND openInterest >= 1000. Rationale: weekly contracts with
    DTE < 3d amplify the annualization factor (0.02% basis over 0.3d = 20%
    annualized from pure quote noise). Thin-OI contracts also have unreliable
    marks. We check the liquid curve only."""
    d = _get(host, "/api/basis/deribit")
    if not d or d.get("status") == "warming":
        return False, "no response or warming"
    by_exp = d.get("byExpiry") or []
    if len(by_exp) < 3:
        return False, f"only {len(by_exp)} expiries - insufficient for monotonicity check"
    valid = [b for b in by_exp
             if b.get("basisAnnualizedPct") is not None
             and b.get("dte", 0) >= 3
             and (b.get("openInterest") or 0) >= 1000]
    if len(valid) < 3:
        return True, (f"only {len(valid)} expiries meet DTE>=3 & OI>=1000 - "
                      f"check skipped (thin market)")
    valid.sort(key=lambda x: x["dte"])
    jumps = []
    for a, b in zip(valid, valid[1:]):
        delta = abs(b["basisAnnualizedPct"] - a["basisAnnualizedPct"])
        if delta > 5.0:
            jumps.append(f"{a['expiry']}->{b['expiry']}: delta={delta:.1f}pp "
                         f"({a['basisAnnualizedPct']:.2f}->{b['basisAnnualizedPct']:.2f})")
    if jumps:
        return False, f"jumps >5pp in liquid curve: {jumps[:3]}"
    fmt = " -> ".join(f"{b['dte']:.0f}d:{b['basisAnnualizedPct']:+.1f}%" for b in valid[:6])
    return True, f"{len(valid)} liquid expiries curve ok: {fmt}"


@check("etf_flows_total_equals_issuers_sum")
def check_etf_total(host):
    """For each day in the Farside table: total should equal Σ per-issuer
    within 2 M$ tolerance (accounting for rounding)."""
    d = _get(host, "/api/etf/flows")
    if not d:
        return False, "no response"
    if d.get("status") == "warming":
        return False, "still warming"
    daily = d.get("daily") or []
    if not daily:
        return False, "daily empty"
    bad = []
    checked = 0
    for day in daily:
        total = day.get("total")
        by = day.get("byIssuer") or {}
        if total is None or not by:
            continue
        issued_sum = sum(v for v in by.values() if v is not None)
        if abs(issued_sum - total) > 2.0:
            bad.append(f"{day['date']}: total={total} vs Σissuers={issued_sum:.2f} (Δ={total - issued_sum:+.2f})")
        checked += 1
    if checked == 0:
        return False, "no days had both total and issuer breakdown"
    if bad:
        return False, f"{len(bad)}/{checked} days inconsistent: {bad[:3]}"
    return True, f"{checked} days consistent; source={d.get('source')} issuers={len(d.get('issuers', []))}"


@check("hl_whales_shape")
def check_hl_whales(host):
    """The hyperliquidWhales response must include positions with the spot
    context fields wired in CL `ea7f365`: `mainnetEth`, `totalSpotEth`, `hedgeLabel`.
    Without these the frontend table renders garbage. We do NOT assert the
    mainnet value is > 0 (depends on whether ETHERSCAN_API_KEY is set in env);
    we only assert the field is present and is the right type."""
    d = _get(host, "/api/data")
    if not d:
        return False, "no response"
    hl = d.get("hyperliquidWhales") or {}
    polled = hl.get("polled")
    if polled is None:
        return False, "hyperliquidWhales.polled missing"
    positions = hl.get("positions") or []
    if not positions:
        return True, f"no open ETH positions among {polled} polled whales (acceptable — depends on market)"
    required = {"mainnetEth", "totalSpotEth", "spotUethEth", "hedgeLabel", "side", "sizeEth"}
    missing_by_pos = []
    for i, p in enumerate(positions):
        missing = required - set(p.keys())
        if missing:
            missing_by_pos.append(f"pos[{i}] missing {sorted(missing)}")
    if missing_by_pos:
        return False, "; ".join(missing_by_pos[:3])
    # Type sanity: mainnetEth must be numeric (could be 0.0 if env key not set)
    if not all(isinstance(p.get("mainnetEth"), (int, float)) for p in positions):
        return False, "mainnetEth is not numeric on at least one position"
    # hedgeLabel must be one of the documented enum values
    valid_labels = {"FULLY_HEDGED", "PARTIAL_HEDGE", "DIRECTIONAL_BET", "DOUBLE_BULL", None}
    bad_label = [p["hedgeLabel"] for p in positions if p.get("hedgeLabel") not in valid_labels]
    if bad_label:
        return False, f"unknown hedgeLabel values: {bad_label[:3]}"
    return True, f"{len(positions)} positions, all fields present, labels valid"


# ── Runner ───────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="http://localhost:8000",
                        help="Base URL (default: http://localhost:8000)")
    parser.add_argument("--wait", type=int, default=0,
                        help="Seconds to poll /api/health for warm caches before running")
    args = parser.parse_args()

    print(f"Smoke tests vs {args.host}")
    print("=" * 60)

    if args.wait > 0:
        deadline = time.time() + args.wait
        while time.time() < deadline:
            h = _get(args.host, "/api/health")
            if h and h.get("etf_age") and h.get("risk_free_age") and h.get("stables_age"):
                print(f"All caches warm after {args.wait - (deadline - time.time()):.0f}s")
                break
            time.sleep(3)

    # Run
    check_riskfree(args.host)
    check_skew(args.host)
    check_stables(args.host)
    check_basis_deribit(args.host)
    check_etf_total(args.host)
    check_hl_whales(args.host)

    print()
    print("=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = [(n, m) for n, ok, m in results if not ok]
    print(f"Result: {passed}/{len(results)} passed")
    if failed:
        print("Failures:")
        for n, m in failed:
            print(f"  - {n}: {m}")
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
