"""Build backtest/REPORTS_SUMMARY.md from reports/*.json + reports/_archive_pre_purge/*.json.

For each preset, read the new report (post PROMPT-A fixes + extended sample)
and emit a 4h-horizon table row + a verdict. Compare against the archived run.
"""
import json
import os
from pathlib import Path

REPORTS = Path("reports")
ARCHIVE = REPORTS / "_archive_pre_purge"
OUT = Path("backtest/REPORTS_SUMMARY.md")

PRESETS = [
    "funding_hot_inflow",
    "funding_hot_outflow",
    "funding_hot_abs",
    "funding_extreme_inflow",
    "funding_extreme_outflow",
    "funding_extreme_abs",
    "stables_expanding",
    "stables_contracting",
    "etf_buying",
    "etf_selling",
]


def verdict(h_block: dict) -> str:
    if not h_block or h_block.get("note") == "insufficient":
        return "insufficient"
    n_valid = h_block.get("n_valid", 0)
    if n_valid < 30:
        return "insufficient"
    ci = h_block.get("ci95_bootstrap") or [None, None]
    if ci[0] is None or ci[1] is None:
        return "no_edge"
    ic = h_block.get("ic_spearman") or 0.0
    p = h_block.get("ic_pvalue_bootstrap")
    dsr = h_block.get("deflated_sharpe") or 0.0
    ci_excludes_zero = (ci[0] > 0 and ci[1] > 0) or (ci[0] < 0 and ci[1] < 0)
    if ci_excludes_zero and abs(ic) > 0.05 and (p is not None and p < 0.05) and dsr > 0.7:
        return "edge"
    if ci_excludes_zero:
        return "weak"
    return "no_edge"


def fmt(v, spec=".4f"):
    if v is None:
        return "-"
    try:
        return format(v, spec)
    except Exception:
        return "-"


def load_report(path: Path) -> dict | None:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    new_rows = []
    archive_rows = []
    for sig in PRESETS:
        new = load_report(REPORTS / f"{sig}.json")
        old = load_report(ARCHIVE / f"{sig}.json")  # may be None for net-new signals

        if new is None:
            new_rows.append((sig, "MISSING", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-"))
            continue

        n_obs = new.get("n_observations") or 0
        n_pre = new.get("n_events_pre_purge") if new.get("n_events_pre_purge") is not None else n_obs
        n_purged = new.get("n_events_purged", 0) or 0
        h4 = (new.get("horizons") or {}).get("4h") or {}
        n_valid = h4.get("n_valid", 0)

        mean_r = h4.get("mean_return")
        ci = h4.get("ci95_bootstrap") or [None, None]
        ic = h4.get("ic_spearman")
        p = h4.get("ic_pvalue_bootstrap")
        hit = h4.get("hit_rate")
        base = h4.get("hit_rate_baseline_random")
        sharpe_evf = h4.get("sharpe_annualized_by_event_freq")
        dsr = h4.get("deflated_sharpe")
        v = verdict(h4)

        new_rows.append((sig, n_pre, n_purged, n_valid, mean_r, ci, ic, p, hit, base, sharpe_evf, dsr, v))

        if old is not None:
            ho4 = (old.get("horizons") or {}).get("4h") or {}
            archive_rows.append((sig,
                                 old.get("n_observations") or 0,
                                 ho4.get("n_valid", 0),
                                 ho4.get("mean_return"),
                                 ho4.get("ci95_bootstrap") or [None, None],
                                 ho4.get("ic_spearman"),
                                 ho4.get("ic_pvalue_bootstrap"),
                                 ho4.get("sharpe_annualized"),
                                 ho4.get("deflated_sharpe"),
                                 verdict(ho4)))
        else:
            archive_rows.append((sig, "(net-new)", "-", "-", "-", "-", "-", "-", "-", "-"))

    # ── Render ────────────────────────────────────────────────────────────
    lines = []
    lines.append("# Reports summary — POST PROMPT-A fixes + extended sample\n")
    lines.append("Generated automatically by `scripts/build_reports_summary.py` from")
    lines.append("`reports/*.json`. Compares against `reports/_archive_pre_purge/`.\n")
    lines.append("Setup applied to every report in this batch:")
    lines.append("- purge_overlapping_events=True (FIX 1)")
    lines.append("- sharpe_per_event + sharpe_annualized_by_event_freq (FIX 2)")
    lines.append("- DSR uses sharpe_per_event with n_obs=n_valid (FIX 2)")
    lines.append("- n_trials=10 (auto-tracked, FIX 3 — the full search width)")
    lines.append("- Klines+funding extended to 5y (FASE B1)")
    lines.append("- Farside ETF unblocked (FASE B2)")
    lines.append("")

    lines.append("## Aggregate event study at 4h horizon — new runs\n")
    lines.append("| signal | n_events | n_purged | n_valid | 4h mean | 4h CI95 | IC | p | hit | base | sharpe_evf | DSR | verdict |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for sig, n_pre, n_purged, n_valid, mean_r, ci, ic, p, hit, base, sh, dsr, v in new_rows:
        ci_str = f"[{fmt(ci[0])}, {fmt(ci[1])}]" if (ci[0] is not None) else "-"
        lines.append(f"| `{sig}` | {n_pre} | {n_purged} | {n_valid} | {fmt(mean_r)} | {ci_str} | "
                     f"{fmt(ic, '.3f')} | {fmt(p, '.3f')} | {fmt(hit, '.3f')} | "
                     f"{fmt(base, '.3f')} | {fmt(sh, '+.2f')} | {fmt(dsr, '.3f')} | **{v}** |")

    lines.append("\n## Same row from the archived (pre-purge) runs\n")
    lines.append("| signal | n_obs (old) | n_valid (old) | 4h mean (old) | CI95 (old) | IC (old) | p (old) | sharpe_ann (old) | DSR (old) | verdict (old) |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for row in archive_rows:
        sig = row[0]
        if row[1] == "(net-new)":
            lines.append(f"| `{sig}` | (no archive — signal newly unblocked or freshly added) | - | - | - | - | - | - | - | - |")
            continue
        sig, n_obs, n_valid, mean_r, ci, ic, p, sh, dsr, v = row
        ci_str = f"[{fmt(ci[0])}, {fmt(ci[1])}]" if (ci[0] is not None) else "-"
        lines.append(f"| `{sig}` | {n_obs} | {n_valid} | {fmt(mean_r)} | {ci_str} | "
                     f"{fmt(ic, '.3f')} | {fmt(p, '.3f')} | {fmt(sh, '+.2f')} | "
                     f"{fmt(dsr, '.3f')} | {v} |")

    lines.append("\n## How the numbers moved\n")
    lines.append("- **Sharpe values are not directly comparable.** Old reports used")
    lines.append("  `HORIZON_PERIODS_PER_YEAR` annualization (8760 for 1h, 365 for 1d),")
    lines.append("  which assumes the strategy could re-fire every horizon period. New")
    lines.append("  reports use `events_per_year` annualization, which is operationally")
    lines.append("  honest. For a signal with 77 events in 2y, the new Sharpe is the")
    lines.append("  old × sqrt(77/8760) ≈ 0.094× — a ~10× compression that brings them")
    lines.append("  into the realm of reasonable strategy Sharpes (0–3) instead of")
    lines.append("  fantasy values like ±17.")
    lines.append("- **DSR moves from saturated to discriminating.** The previous code")
    lines.append("  fed the annualized Sharpe into the deflated-Sharpe formula whose")
    lines.append("  variance term assumes per-period Sharpe; that mismatch pinned DSR")
    lines.append("  at 1.0 on most reports regardless of true edge. With the per-event")
    lines.append("  Sharpe input + n_trials=10 (vs the old 1), DSR now actually")
    lines.append("  separates real edges from search noise.")
    lines.append("- **n_events shrinks where purge bites.** Funding signals fire often")
    lines.append("  enough that the 7d-horizon purge drops a meaningful fraction;")
    lines.append("  stables fire ~monthly so the purge is mostly a no-op. The new")
    lines.append("  `n_observations` in the JSON reflects pre-purge count; `n_valid`")
    lines.append("  in the per-horizon block reflects post-purge usable events.")

    lines.append("\n## Verdict legend\n")
    lines.append("- **edge**: CI95 excludes 0 AND |IC|>0.05 AND p<0.05 AND DSR>0.7 — actionable.")
    lines.append("- **weak**: CI95 excludes 0 but at least one of {IC, p, DSR} is below threshold.")
    lines.append("- **no_edge**: CI95 spans 0 — can't reject no-effect at 95%.")
    lines.append("- **insufficient**: n_valid < 30 — sample too small for stable stats.")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
