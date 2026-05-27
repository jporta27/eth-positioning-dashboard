"""
HMM K=4 regime classifier for ETH 4h.

Produces, given the recent feature window:
  - Current regime label and confidence (forward-backward posterior)
  - Full P(state) distribution
  - Expected remaining dwell time
  - Multi-step transition forecast P^h for several horizons

Design choices (all justified in docs/obsidian/03-metrics/Regime classifier.md):

  K=4 — BIC strictly prefers K=5 over K=4 (Δ=5,427), but the K=5 5th state has
        max |mean| = 0.156 across all 8 features (an EM-induced "centered"
        artifact, not an interpretable regime). K=4 is the parsimonious choice.

  Rolling re-fit — Test 2 of the robustness suite showed CRASH and STRESS
        state vol signatures drift 0.77-0.79σ across rolling 18m windows.
        A stationary model trained once will progressively misclassify as
        vol regime evolves. Re-fit every REFIT_EVERY_DAYS.

  Diagonal covariance — K(K-1) + K(2d) + K-1 = 79 params. Full covariance
        would be 4(K-1) + K(d²+d)/2 = 159 params for K=4, d=8. Diagonal is
        ~half the param count with comparable likelihood in our setting.

  Labels in canonical order — CRASH/STRESS/CHOP/UP. Identity is assigned
        post-fit by sorting raw HMM states first by realized_vol_24 desc,
        then by log_return_4h within vol pairs. This guarantees that after
        every refit, the same label corresponds to the same conceptual regime
        (high-vol-bearish → CRASH, etc.) even though hmmlearn assigns
        arbitrary integer indices.

States (always in this order in the API):
  CRASH  — mid-vol bearish drift (vol ≈ +0.4σ, returns slightly negative)
  STRESS — high-vol expansion (vol > +1σ, VIX elevated, funding often
           negative — liquidation cascade or melt-up)
  CHOP   — calm consolidation (vol < -1σ, returns ≈ 0)
  UP     — calm bull grind (vol moderately low, slight positive return drift)

This module is consumed by:
  - scripts/run_regime_classifier.py — CLI (refit / classify)
  - backend/main.py — fetch_regime() coroutine (warm cache, served at /api/regime)
  - api/index.py — same, Vercel mirror

It MUST not import any backend/api stateful objects to remain serializable
and testable in isolation.
"""

from __future__ import annotations

import os
import pickle
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np
import pandas as pd
from hmmlearn import hmm


# ── Canonical labels and feature names ──────────────────────────────
LABELS = ["CRASH", "STRESS", "CHOP", "UP"]
K = 4
FEATURE_NAMES = [
    "log_return_4h", "realized_vol_24", "parkinson_vol_24",
    "taker_buy_imbalance", "return_skew_20", "funding_zscore",
    "eth_btc_relret_20", "vix_level_z",
]

# ── Hyperparameters ─────────────────────────────────────────────────
# All thresholds are named constants per CLAUDE.md rule 3.
COV_TYPE = "diag"              # diagonal covariance keeps param count bounded
N_RESTARTS = 20                # EM random starts per fit (median-bestlikelihood is stable above ~15)
WINDOW_MONTHS = 18             # rolling-fit history window
REFIT_EVERY_DAYS = 7           # refit cadence (Test 2 drift develops on ~3-month timescale; 7d is overkill, OK)
ROLL_WIN_24 = 24               # ~4 days at 4h cadence
ROLL_WIN_20 = 20               # ~3.3 days
STANDARDIZE_WIN = 252          # ~6 weeks for rolling z-scores in features
HORIZONS = (1, 6, 24, 48)      # transition forecast horizons (4h, 24h, 4d, 8d)


# ── Feature engineering ─────────────────────────────────────────────
def parkinson_vol(high: pd.Series, low: pd.Series, window: int) -> pd.Series:
    """Parkinson's range-based vol estimator.

    σ̂ = sqrt(Σ ln(H/L)² / (4·n·ln 2)). Less noisy than close-to-close on
    typical OHLC data because it uses the full bar's range, not just close.
    """
    ratio = np.log(high / low) ** 2
    return np.sqrt(ratio.rolling(window).sum() / (4 * window * np.log(2)))


def causal_zscore(s: pd.Series, window: int) -> pd.Series:
    """Rolling z-score using only past data (shift(1) prevents using bar t to
    standardize bar t — that would be lookahead bias)."""
    mu = s.shift(1).rolling(window).mean()
    sd = s.shift(1).rolling(window).std()
    return (s - mu) / sd


def build_features(klines_4h: pd.DataFrame, funding_4h: pd.Series,
                   macro_4h: pd.DataFrame) -> pd.DataFrame:
    """Build the 8-feature matrix at 4h cadence.

    Inputs assumed already on a 4h DatetimeIndex (UTC). Funding (8h cadence)
    and macro (1d cadence) must be forward-filled to 4h BEFORE calling this
    — that's the caller's responsibility because they may want different
    fill policies (e.g. raise on missing macro vs ffill silently).
    """
    f = pd.DataFrame(index=klines_4h.index)
    f["log_return_4h"] = klines_4h["log_return"]
    f["realized_vol_24"] = klines_4h["log_return"].rolling(ROLL_WIN_24).std()
    f["parkinson_vol_24"] = parkinson_vol(klines_4h["high"], klines_4h["low"], ROLL_WIN_24)
    f["taker_buy_imbalance"] = klines_4h["taker_buy_imbalance"]
    f["return_skew_20"] = klines_4h["log_return"].rolling(ROLL_WIN_20).skew()
    f["funding_zscore"] = causal_zscore(funding_4h.reindex(f.index, method="ffill"), STANDARDIZE_WIN)
    btc = macro_4h["btc_close"].reindex(f.index, method="ffill")
    btc_logret = np.log(btc).diff()
    f["eth_btc_relret_20"] = klines_4h["log_return"].rolling(ROLL_WIN_20).sum() - btc_logret.rolling(ROLL_WIN_20).sum()
    vix = macro_4h["vix_close"].reindex(f.index, method="ffill")
    f["vix_level_z"] = causal_zscore(vix, STANDARDIZE_WIN)
    return f.dropna()


# ── Canonical label assignment ──────────────────────────────────────
def assign_canonical_labels(state_means: pd.DataFrame) -> dict:
    """Map raw HMM state indices (0..3) to canonical labels (CRASH/STRESS/CHOP/UP).

    Procedure (deterministic, no human judgment):
      1. Sort states by realized_vol_24 descending → top 2 are high-vol, bottom 2 calm
      2. Within each pair, sort by log_return_4h descending → upper is bull-flavored
      3. High-vol pair:  bull-flavored = STRESS (high vol + positive ret = melt-up),
                         bear-flavored = CRASH  (high vol + negative ret)
      4. Low-vol pair:   bull-flavored = UP     (calm bull grind),
                         bear-flavored = CHOP   (sideways consolidation)

    Why vol-first, return-second: in our K=4 sweep the primary structural axis
    of separation was vol regime, not return sign. Sorting purely by return
    misclassifies (e.g. the K=4 STRESS state has positive mean return because
    melt-up squeezes lift price; it would mislabel as "UP" if sorted by return).
    """
    by_vol = state_means.sort_values("realized_vol_24", ascending=False)
    high_vol = by_vol.iloc[:2].sort_values("log_return_4h", ascending=False)
    low_vol = by_vol.iloc[2:].sort_values("log_return_4h", ascending=False)
    out = {}
    out[int(high_vol.index[0])] = "STRESS"   # high vol + higher ret
    out[int(high_vol.index[1])] = "CRASH"    # high vol + lower ret
    out[int(low_vol.index[0])] = "UP"        # low vol + higher ret
    out[int(low_vol.index[1])] = "CHOP"      # low vol + lower ret
    return out


# ── HMM fit with restarts ───────────────────────────────────────────
def _fit_hmm_with_restarts(X: np.ndarray, n_restarts: int = N_RESTARTS,
                            cov_type: str = COV_TYPE, rng_seed: int = 42) -> hmm.GaussianHMM:
    """Fit Gaussian HMM with multiple random starts; return the model with the
    highest converged log-likelihood. EM has multiple local maxima — without
    restarts we may settle in a suboptimal solution."""
    best_model = None
    best_ll = -np.inf
    rng = np.random.default_rng(rng_seed)
    for _ in range(n_restarts):
        m = hmm.GaussianHMM(
            n_components=K,
            covariance_type=cov_type,
            n_iter=200,
            tol=1e-4,
            random_state=int(rng.integers(0, 2**31 - 1)),
            init_params="stmc",
        )
        try:
            m.fit(X)
            ll = m.score(X)
            if ll > best_ll:
                best_ll, best_model = ll, m
        except Exception:
            continue
    if best_model is None:
        raise RuntimeError("HMM fit failed on every restart")
    return best_model


# ── Serializable artifact ───────────────────────────────────────────
@dataclass
class FittedRegimeArtifact:
    """The full pickled artifact: model + standardization stats + labels +
    metadata about when and on what data it was fit. Restored by load()."""
    hmm_model: hmm.GaussianHMM
    feature_means: np.ndarray         # per-feature mean used to z-score (length d=8)
    feature_stds: np.ndarray          # per-feature std (length d=8)
    labels_by_idx: dict               # {0: "CRASH", 1: "STRESS", ...} — varies by run
    transition_matrix: np.ndarray     # canonical order: [CRASH, STRESS, CHOP, UP]
    fit_date: str                     # ISO date string
    train_start: str
    train_end: str
    train_n_obs: int
    train_logL: float


# ── Main classifier class ───────────────────────────────────────────
class RegimeClassifier:
    """Stateful HMM regime classifier. Re-fits periodically as the vol regime
    drifts (per Test 2 of robustness suite)."""

    def __init__(self, artifact: Optional[FittedRegimeArtifact] = None):
        self.artifact = artifact

    # ── Fitting ─────────────────────────────────────────────────────
    @classmethod
    def fit(cls, features: pd.DataFrame, window_months: int = WINDOW_MONTHS,
            n_restarts: int = N_RESTARTS, end_date: Optional[pd.Timestamp] = None) -> "RegimeClassifier":
        """Fit on the most recent `window_months` of features. `end_date` lets
        you simulate historical fits (default: features.index.max())."""
        if end_date is None:
            end_date = features.index.max()
        start = end_date - pd.DateOffset(months=window_months)
        train = features[(features.index >= start) & (features.index <= end_date)]
        if len(train) < 1000:
            raise ValueError(f"Need at least 1000 4h bars to fit; got {len(train)}")

        # Standardize using TRAIN stats only. Persist mu, sigma for later transform.
        mu = train.mean().values
        sd = train.std().replace(0, 1.0).values
        X = (train.values - mu) / sd

        model = _fit_hmm_with_restarts(X, n_restarts=n_restarts)
        train_logL = model.score(X)

        # Canonical label assignment
        states = model.predict(X)
        state_means = pd.DataFrame(X, columns=train.columns)
        state_means["state"] = states
        per_state_means = state_means.groupby("state").mean()
        labels_by_idx = assign_canonical_labels(per_state_means)

        # Build canonical transition matrix (rows/cols in LABELS order)
        # Note: smoothing makes a tiny difference but the artifact captures the
        # raw model's transmat_ for full reproducibility.
        raw_P = model.transmat_
        idx_by_label = {v: k for k, v in labels_by_idx.items()}
        order = [idx_by_label[s] for s in LABELS]
        P_canon = raw_P[np.ix_(order, order)]

        artifact = FittedRegimeArtifact(
            hmm_model=model,
            feature_means=mu,
            feature_stds=sd,
            labels_by_idx=labels_by_idx,
            transition_matrix=P_canon,
            fit_date=datetime.now(timezone.utc).isoformat(),
            train_start=str(train.index.min()),
            train_end=str(train.index.max()),
            train_n_obs=len(train),
            train_logL=float(train_logL),
        )
        return cls(artifact=artifact)

    # ── Classification ──────────────────────────────────────────────
    def needs_refit(self, refit_every_days: int = REFIT_EVERY_DAYS) -> bool:
        """True if the artifact is older than the refit cadence."""
        if self.artifact is None:
            return True
        fit_dt = datetime.fromisoformat(self.artifact.fit_date)
        age = datetime.now(timezone.utc) - fit_dt
        return age > timedelta(days=refit_every_days)

    def classify(self, features: pd.DataFrame) -> dict:
        """Classify the most recent bar of features (and the trailing history
        needed by the HMM forward pass). Returns the dashboard-ready dict.

        Strategy: forward-backward over the full passed-in window so that
        the LAST bar's posterior reflects the entire context, not just the
        single observation. This is what makes the HMM useful vs a Gaussian
        Naive Bayes (which would only use the bar's features in isolation).
        """
        if self.artifact is None:
            raise RuntimeError("Classifier not fit yet")
        if features.empty:
            raise ValueError("Empty features dataframe")

        # Standardize with TRAIN stats (no data leakage from current bars)
        X = (features.values - self.artifact.feature_means) / self.artifact.feature_stds

        # Forward-backward: smoothed posteriors P(state_t | obs_1..T) for all t
        # We take the LAST row as "current".
        post = self.artifact.hmm_model.predict_proba(X)
        last_post = post[-1]  # shape (K,)

        # Reorder to canonical label order
        idx_by_label = {v: k for k, v in self.artifact.labels_by_idx.items()}
        canonical_post = np.array([last_post[idx_by_label[s]] for s in LABELS])
        current_idx = int(np.argmax(canonical_post))
        current_label = LABELS[current_idx]
        confidence = float(canonical_post[current_idx])
        # Shannon entropy of the posterior, in nats
        entropy = float(-np.sum(canonical_post * np.log(canonical_post + 1e-12)))

        # Expected remaining dwell time in current regime under the canonical P
        diag = self.artifact.transition_matrix[current_idx, current_idx]
        expected_dwell_bars = 1.0 / max(1e-9, 1.0 - diag)

        # Multi-step transition forecast P^h
        forecast = {}
        for h in HORIZONS:
            Ph = np.linalg.matrix_power(self.artifact.transition_matrix, h)
            # Row corresponding to current regime
            row = Ph[current_idx]
            forecast[f"{h}bar"] = {LABELS[j]: float(row[j]) for j in range(K)}

        # Model age in days (helps UI decide when to nag for refit)
        fit_dt = datetime.fromisoformat(self.artifact.fit_date)
        model_age_days = (datetime.now(timezone.utc) - fit_dt).total_seconds() / 86400.0

        last_ts = features.index[-1]
        last_ts_ms = int(last_ts.timestamp() * 1000) if hasattr(last_ts, "timestamp") else None

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "lastBarTs": last_ts_ms,
            "modelFitDate": self.artifact.fit_date,
            "modelAgeDays": round(model_age_days, 2),
            "modelTrainEnd": self.artifact.train_end,
            "modelTrainNObs": self.artifact.train_n_obs,
            "currentState": current_label,
            "confidence": round(confidence, 4),
            "entropy": round(entropy, 4),
            "probabilities": {LABELS[j]: round(float(canonical_post[j]), 4) for j in range(K)},
            "expectedDwellBars": round(expected_dwell_bars, 2),
            "expectedDwellHours": round(expected_dwell_bars * 4, 1),
            "transitionForecast": forecast,
            "transitionMatrix": {
                LABELS[i]: {LABELS[j]: round(float(self.artifact.transition_matrix[i, j]), 4)
                            for j in range(K)}
                for i in range(K)
            },
        }

    # ── Persistence ─────────────────────────────────────────────────
    def save(self, path: str) -> None:
        """Pickle the fitted artifact to disk. Path should end in .pkl."""
        if self.artifact is None:
            raise RuntimeError("Nothing to save — classifier not fit")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(self.artifact, f)

    @classmethod
    def load(cls, path: str) -> "RegimeClassifier":
        """Restore a previously saved classifier."""
        with open(path, "rb") as f:
            artifact = pickle.load(f)
        return cls(artifact=artifact)
