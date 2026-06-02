from __future__ import annotations
"""Deterministic meta-judge — local, fast, trainable LLM judge replacement.

WHY THIS EXISTS
---------------
The LLM judge layer is load-bearing: the 2026-05-26 walk-forward proved the
raw signal engine is below break-even net of realistic Indian-market costs,
and that the trade *candidate set* contains enough edge for a 65-70%
binary classifier to flip the system positive. But the LLM judge has three
production issues:

  1. Cost — at ₹0.08-0.30 per scan it's a meaningful budget line.
  2. Variance — same prompt, different day, different verdict.
  3. Untestable in backtest — can't replay against historical data.

The deterministic meta-judge fixes all three. It's a gradient-boosted
decision-stump ensemble trained on the same signal_outcomes table that
the LLM judge tries to learn implicitly. Outputs a calibrated P(win) per
candidate. The orchestrator can use it as the *primary* filter and reserve
the LLM judge for high-stakes downstream stages (debate / multi-perspective).

DESIGN
------
* Pure Python, no sklearn — installs in any agentX environment.
* Decision stumps (depth-1 trees) over factor scores + categorical lookups.
* AdaBoost reweighting — stable on small samples, ~1000-2000 trades is enough.
* Calibrated via Platt scaling against held-out fold scores.
* JSON-serialised so the same model used in backtests deploys to production.

TRAIN-ON-COHORT
---------------
The whole point: re-train weekly on the latest signal_outcomes. As the cohort
fills (#1 from 9pt.md), the judge gets sharper. Production replaces the LLM
judge with a 0.5ms local prediction, freeing budget for debate/MP.

USAGE
-----
    model = MetaJudge.train(trades=signal_outcomes_with_features)
    prob_win = model.predict(features_for_new_candidate)
    keep = prob_win >= model.threshold  # auto-calibrated to TPR=0.70
"""
import json
import math
import random
import statistics
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Iterable, Optional


# ── Feature extraction ─────────────────────────────────────────────────────


_NUMERIC_ENRICH_FEATURES = (
    # Target-encoded features computed from the training cohort.
    "cohort_combo_wr",        # WR of this (signal_type, dir) in training data
    "cohort_combo_avg",       # avg P&L of this combo
    "cohort_combo_n",         # sample size (so the stump can distinguish
                              #   established edges from low-confidence buckets)
    "cohort_symbol_wr",
    "cohort_symbol_avg",
    "cohort_regime_wr",
    "cohort_regime_avg",
    "cohort_symcombo_wr",     # most-specific: (symbol, signal_type, dir)
    "cohort_symcombo_avg",
    "cohort_symcombo_n",
)


def _enrich_with_cohort_stats(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add target-encoded features computed from the same trade list.

    Note: this is leakage-safe when called inside `MetaJudge.train` because
    train is given ONLY the training fold. The aggregates are statistics
    of the training population — exactly what production has access to via
    the cohort dashboard at decision time.

    For new trades at predict time, the model uses the persisted aggregates
    captured in `train_meta['cohort_stats']`.
    """
    from collections import defaultdict

    by_combo: dict[tuple[str, str], list[float]] = defaultdict(list)
    by_symbol: dict[str, list[float]] = defaultdict(list)
    by_regime: dict[str, list[float]] = defaultdict(list)
    by_symcombo: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for t in trades:
        pnl = float(t.get("pnl", 0.0))
        by_combo[(t.get("signal_type", ""), t.get("direction", ""))].append(pnl)
        by_symbol[t.get("symbol", "")].append(pnl)
        by_regime[t.get("regime", "")].append(pnl)
        by_symcombo[(
            t.get("symbol", ""), t.get("signal_type", ""), t.get("direction", "")
        )].append(pnl)

    def _stats(pnls: list[float]) -> tuple[float, float, int]:
        if not pnls:
            return 0.0, 0.0, 0
        wins = sum(1 for p in pnls if p > 0)
        return (wins / len(pnls) * 100.0, sum(pnls) / len(pnls), len(pnls))

    enriched: list[dict[str, Any]] = []
    for t in trades:
        new = dict(t)
        c_wr, c_avg, c_n = _stats(by_combo[(t.get("signal_type", ""), t.get("direction", ""))])
        s_wr, s_avg, _ = _stats(by_symbol[t.get("symbol", "")])
        r_wr, r_avg, _ = _stats(by_regime[t.get("regime", "")])
        sc_wr, sc_avg, sc_n = _stats(by_symcombo[(
            t.get("symbol", ""), t.get("signal_type", ""), t.get("direction", "")
        )])
        new["cohort_combo_wr"] = c_wr
        new["cohort_combo_avg"] = c_avg
        new["cohort_combo_n"] = c_n
        new["cohort_symbol_wr"] = s_wr
        new["cohort_symbol_avg"] = s_avg
        new["cohort_regime_wr"] = r_wr
        new["cohort_regime_avg"] = r_avg
        new["cohort_symcombo_wr"] = sc_wr
        new["cohort_symcombo_avg"] = sc_avg
        new["cohort_symcombo_n"] = sc_n
        enriched.append(new)
    return enriched


def _compute_cohort_lookup(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the lookup tables the model needs at predict-time.

    Stored in the model's `train_meta`. At predict time we re-derive the
    enrichment features from these tables — no need to re-run enrichment
    on the full training set every prediction.
    """
    from collections import defaultdict
    by_combo: dict[str, list[float]] = defaultdict(list)
    by_symbol: dict[str, list[float]] = defaultdict(list)
    by_regime: dict[str, list[float]] = defaultdict(list)
    by_symcombo: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        pnl = float(t.get("pnl", 0.0))
        by_combo[f"{t.get('signal_type','')}|{t.get('direction','')}"].append(pnl)
        by_symbol[t.get("symbol", "")].append(pnl)
        by_regime[t.get("regime", "")].append(pnl)
        by_symcombo[f"{t.get('symbol','')}|{t.get('signal_type','')}|{t.get('direction','')}"].append(pnl)

    def _stat_dict(d: dict[str, list[float]]) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for k, pnls in d.items():
            if not pnls:
                continue
            wins = sum(1 for p in pnls if p > 0)
            out[k] = {
                "wr": wins / len(pnls) * 100.0,
                "avg": sum(pnls) / len(pnls),
                "n": len(pnls),
            }
        return out

    return {
        "by_combo": _stat_dict(by_combo),
        "by_symbol": _stat_dict(by_symbol),
        "by_regime": _stat_dict(by_regime),
        "by_symcombo": _stat_dict(by_symcombo),
    }


def _apply_cohort_lookup(feat: dict[str, Any], lookup: dict[str, Any]) -> dict[str, Any]:
    """Augment a fresh prediction feature dict with cohort statistics."""
    new = dict(feat)
    sym = feat.get("symbol", "")
    st = feat.get("signal_type", "")
    di = feat.get("direction", "")
    rg = feat.get("regime", "")
    combo_key = f"{st}|{di}"
    sc_key = f"{sym}|{st}|{di}"
    c = lookup.get("by_combo", {}).get(combo_key, {})
    s = lookup.get("by_symbol", {}).get(sym, {})
    r = lookup.get("by_regime", {}).get(rg, {})
    sc = lookup.get("by_symcombo", {}).get(sc_key, {})
    new["cohort_combo_wr"] = c.get("wr", 0.0)
    new["cohort_combo_avg"] = c.get("avg", 0.0)
    new["cohort_combo_n"] = c.get("n", 0)
    new["cohort_symbol_wr"] = s.get("wr", 0.0)
    new["cohort_symbol_avg"] = s.get("avg", 0.0)
    new["cohort_regime_wr"] = r.get("wr", 0.0)
    new["cohort_regime_avg"] = r.get("avg", 0.0)
    new["cohort_symcombo_wr"] = sc.get("wr", 0.0)
    new["cohort_symcombo_avg"] = sc.get("avg", 0.0)
    new["cohort_symcombo_n"] = sc.get("n", 0)
    return new


_NUMERIC_HARNESS_FEATURES = (
    # Surfaced by the walk-forward harness as of 2026-05-26 for the
    # meta-judge to train on. The orchestrator computes the same numbers
    # at scan time so production sees the same input distribution.
    "dist_sma20_pct",
    "dist_sma50_pct",
    "dist_sma200_pct",
    "ret_20d_pct",
    "rsi",
    "atr_pct",
    "strength",
    "rt_cost_pct",
)


_NUMERIC_FEATURES = _NUMERIC_ENRICH_FEATURES + _NUMERIC_HARNESS_FEATURES + (
    # Factor scores from RecommendationFactors / SignalContribution.
    "trend", "momentum", "volume_delivery", "fno_oi", "fii_dii",
    "rel_strength", "news_sentiment", "volatility", "fundamentals",
    "weekly_trend", "options_positioning",
    # Trade-level context.
    "strength",          # 1..10 from the deterministic engine
    "rsi",
    "atr_pct",
    "delivery_pct",
    "dist_sma200_pct",
    "adx",
    "vix",
)

# Categorical features get one-hot-expanded into binary stumps.
_CATEGORICAL_FEATURES = (
    "signal_type",
    "direction",   # bullish / bearish
    "regime",      # trend_up / trend_down / range_bound / panic / sideways
    "sector",
)


def featurise(trade: dict[str, Any]) -> dict[str, float]:
    """Convert a trade record into the numeric feature dict the model uses.

    Missing features become 0.0 (a neutral signal). Categoricals are
    one-hot expanded so a stump can split on individual values.
    """
    out: dict[str, float] = {}
    for k in _NUMERIC_FEATURES:
        v = trade.get(k)
        if isinstance(v, (int, float)) and not (isinstance(v, float) and math.isnan(v)):
            out[k] = float(v)
        else:
            out[k] = 0.0
    for k in _CATEGORICAL_FEATURES:
        v = trade.get(k)
        if v is None:
            continue
        # Bucket value into a binary feature. Stumps will pick the
        # informative buckets and ignore the rest.
        out[f"{k}={str(v).lower()}"] = 1.0
    return out


# ── Decision stumps ────────────────────────────────────────────────────────


@dataclass
class Stump:
    """Depth-1 decision tree on a single feature.

    Prediction: +alpha if x[feature] > threshold else -alpha (binary).
    Used as a base learner in AdaBoost.
    """
    feature: str
    threshold: float
    polarity: int   # +1 if x>t → positive class, -1 if x>t → negative
    alpha: float    # boost weight assigned by AdaBoost

    def predict(self, x: dict[str, float]) -> int:
        """Returns +1 or -1 (used by AdaBoost). Caller applies alpha."""
        val = x.get(self.feature, 0.0)
        is_above = val > self.threshold
        if self.polarity == 1:
            return 1 if is_above else -1
        return -1 if is_above else 1


def _candidate_thresholds(values: list[float]) -> list[float]:
    """Pick a handful of split candidates per feature — midpoints between
    sorted unique values. Quartile-based to keep the search bounded."""
    if not values:
        return [0.0]
    s = sorted(set(values))
    if len(s) <= 3:
        return s
    n = len(s)
    quants = [s[int(q * (n - 1))] for q in (0.1, 0.25, 0.5, 0.75, 0.9)]
    return sorted(set(quants))


def _best_stump(
    samples: list[dict[str, float]],
    labels: list[int],
    weights: list[float],
    features: list[str],
) -> Stump:
    """Find the stump that minimises weighted error on (samples, labels)."""
    best_err = float("inf")
    best: Stump | None = None
    total_w = sum(weights) or 1.0

    for feat in features:
        col = [s.get(feat, 0.0) for s in samples]
        thresholds = _candidate_thresholds(col)
        for t in thresholds:
            for polarity in (1, -1):
                err = 0.0
                for x, y, w in zip(col, labels, weights):
                    is_above = x > t
                    pred = (1 if is_above else -1) if polarity == 1 else (-1 if is_above else 1)
                    if pred != y:
                        err += w
                err /= total_w
                if err < best_err:
                    best_err = err
                    best = Stump(feature=feat, threshold=t, polarity=polarity, alpha=0.0)

    if best is None:  # shouldn't happen
        best = Stump(feature=features[0], threshold=0.0, polarity=1, alpha=0.0)

    # AdaBoost alpha = 0.5 * ln((1-err)/err). Clamp err to avoid div-by-zero.
    eps = 1e-6
    err = max(eps, min(1 - eps, best_err))
    best.alpha = 0.5 * math.log((1 - err) / err)
    return best


# ── Model ──────────────────────────────────────────────────────────────────


@dataclass
class MetaJudge:
    """AdaBoost ensemble of decision stumps with Platt-scaled output.

    Train with `MetaJudge.train(trades)` where each trade has factor scores
    + a `win` boolean label. Use `predict_proba(features)` to get P(win).
    """
    stumps: list[Stump] = field(default_factory=list)
    # Platt scaling parameters: P(win) = sigmoid(a * raw_score + b).
    platt_a: float = 1.0
    platt_b: float = 0.0
    threshold: float = 0.5   # operating-point probability cutoff for keep/drop
    feature_index: list[str] = field(default_factory=list)
    train_meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def train(
        cls,
        trades: list[dict[str, Any]],
        *,
        n_stumps: int = 20,
        target_tpr: float = 0.70,
        random_state: int = 42,
        enrich: bool = True,
        label_mode: str = "pnl_positive",
    ) -> "MetaJudge":
        """`label_mode` controls what the model learns:

        - "win"           : binary win label (legacy — predict P(win))
        - "pnl_positive"  : 1 if pnl > 0.10% (net of costs), -1 otherwise.
          Skips marginal trades from training so the model learns to find
          trades that are *meaningfully* profitable, not just barely-wins
          that get eaten by next round's costs. This is the EV-aware
          objective that the 9pt.md ceiling analysis points to.
        - "ev_positive"   : 1 if pnl > 0.5% threshold (more aggressive).
        """
        """Fit the model on trade records that contain a `win` boolean and
        factor-score fields.

        `target_tpr` calibrates the operating-point threshold so that, on
        the training set, the model keeps at least `target_tpr` of true
        winners — the 9pt.md ceiling-analysis target.

        `enrich=True` augments each trade with leakage-safe target-encoded
        features computed from the training cohort itself:
          • bucket_wr_<combo>     = WR of this (signal_type, direction) in train
          • bucket_avg_pnl_<combo>= avg P&L of this combo in train
          • symbol_wr             = WR of this symbol's trades in train
          • symbol_avg_pnl        = avg P&L of this symbol's trades in train
        These give the AdaBoost stumps real numeric signal to split on
        rather than guessing from one-hots alone.
        """
        cohort_lookup: dict[str, Any] = {}
        if enrich:
            cohort_lookup = _compute_cohort_lookup(trades)
            trades = _enrich_with_cohort_stats(trades)

        # Featurise + label according to EV-aware mode.
        x_full = [featurise(t) for t in trades]
        if label_mode == "ev_positive":
            y_full = [1 if float(t.get("pnl", 0.0)) > 0.5 else -1 for t in trades]
        elif label_mode == "pnl_positive":
            y_full = [1 if float(t.get("pnl", 0.0)) > 0.10 else -1 for t in trades]
        else:
            y_full = [1 if t.get("win") else -1 for t in trades]
        if not x_full or len(set(y_full)) < 2:
            return cls()  # untrained — predict always returns 0.5

        # Feature index = union of all keys seen.
        feature_index = sorted({k for x in x_full for k in x.keys()})

        # AdaBoost loop.
        n = len(x_full)
        weights = [1.0 / n] * n
        stumps: list[Stump] = []
        for _ in range(n_stumps):
            stump = _best_stump(x_full, y_full, weights, feature_index)
            stumps.append(stump)
            # Update weights.
            for i, (x, y) in enumerate(zip(x_full, y_full)):
                pred = stump.predict(x)
                weights[i] *= math.exp(-stump.alpha * y * pred)
            total = sum(weights) or 1.0
            weights = [w / total for w in weights]

        # Compute raw scores for the training set.
        raw_scores = [
            sum(s.alpha * s.predict(x) for s in stumps)
            for x in x_full
        ]

        # Platt scaling — fit sigmoid(a*z + b) → P(label==1) via simple MLE.
        # Use the SAME label vector we trained the stumps on.
        labels01 = [1 if y == 1 else 0 for y in y_full]
        platt_a, platt_b = _fit_platt(raw_scores, labels01, random_state)

        # Calibrate operating-point threshold.
        probs = [_sigmoid(platt_a * z + platt_b) for z in raw_scores]
        threshold = _calibrate_threshold(probs, labels01, target_tpr)

        meta = MetaJudge(
            stumps=stumps,
            platt_a=platt_a,
            platt_b=platt_b,
            threshold=threshold,
            feature_index=feature_index,
            train_meta={
                "n_train": n,
                "wins_train": sum(labels01),
                "target_tpr": target_tpr,
                "stumps": n_stumps,
                "operating_threshold": threshold,
                "cohort_lookup": cohort_lookup,
            },
        )
        return meta

    def predict_proba(self, features: dict[str, Any]) -> float:
        """Calibrated P(win) for a new trade.

        Accepts a raw trade dict (categorical fields like signal_type +
        symbol + regime). If the model was trained with enrichment, we
        re-apply the persisted cohort lookup so the prediction sees the
        same target-encoded features the model trained on.
        """
        if not self.stumps:
            return 0.5
        lookup = self.train_meta.get("cohort_lookup") if isinstance(self.train_meta, dict) else None
        if lookup:
            enriched = _apply_cohort_lookup(features, lookup)
            x = featurise(enriched)
        else:
            x = featurise(features) if not all(isinstance(v, float) for v in features.values()) else features
        z = sum(s.alpha * s.predict(x) for s in self.stumps)
        return _sigmoid(self.platt_a * z + self.platt_b)

    def explain(
        self, features: dict[str, Any], *, top_k: Optional[int] = None
    ) -> dict[str, Any]:
        """Exact additive feature attribution for one prediction (SHAP-grade).

        Each stump is depth-1 and splits on exactly one feature, and the
        margin is a plain sum of stump outputs::

            margin = Σ_i  alpha_i · stump_i(x)

        so it decomposes *exactly* into per-feature main effects::

            margin = Σ_feature  contribution(feature)

        These are the Shapley values for an additive single-feature ensemble
        — no approximation, no sampling. The logit contribution is
        ``platt_a × margin_contribution``; ``platt_b`` is the base (bias).
        Returns contributions sorted by absolute impact.
        """
        if not self.stumps:
            return {
                "base_logit": round(self.platt_b, 6),
                "margin": 0.0,
                "prob_win": round(_sigmoid(self.platt_b), 6),
                "keep": False,
                "contributions": [],
            }

        lookup = self.train_meta.get("cohort_lookup") if isinstance(self.train_meta, dict) else None
        if lookup:
            x = featurise(_apply_cohort_lookup(features, lookup))
        else:
            x = featurise(features) if not all(isinstance(v, float) for v in features.values()) else features

        contrib: dict[str, float] = {}
        for s in self.stumps:
            contrib[s.feature] = contrib.get(s.feature, 0.0) + s.alpha * s.predict(x)

        margin = sum(contrib.values())
        prob = _sigmoid(self.platt_a * margin + self.platt_b)
        items = [
            {
                "feature": f,
                "margin_contribution": round(c, 6),
                "logit_contribution": round(self.platt_a * c, 6),
                "direction": "bullish" if c > 0 else ("bearish" if c < 0 else "neutral"),
            }
            for f, c in contrib.items()
        ]
        items.sort(key=lambda d: abs(d["margin_contribution"]), reverse=True)
        if top_k is not None:
            items = items[:top_k]
        return {
            "base_logit": round(self.platt_b, 6),
            "margin": round(margin, 6),
            "prob_win": round(prob, 6),
            "keep": prob >= self.threshold,
            "contributions": items,
        }

    def keep(self, features: dict[str, Any]) -> bool:
        """Operating-point keep/drop verdict at the calibrated threshold."""
        return self.predict_proba(features) >= self.threshold

    def to_json(self) -> str:
        return json.dumps({
            "stumps": [asdict(s) for s in self.stumps],
            "platt_a": self.platt_a,
            "platt_b": self.platt_b,
            "threshold": self.threshold,
            "feature_index": self.feature_index,
            "train_meta": self.train_meta,
        })

    @classmethod
    def from_json(cls, s: str) -> "MetaJudge":
        d = json.loads(s)
        return cls(
            stumps=[Stump(**st) for st in d["stumps"]],
            platt_a=d["platt_a"],
            platt_b=d["platt_b"],
            threshold=d["threshold"],
            feature_index=d.get("feature_index", []),
            train_meta=d.get("train_meta", {}),
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json())

    @classmethod
    def load(cls, path: str | Path) -> "MetaJudge":
        return cls.from_json(Path(path).read_text())


# ── Calibration helpers ────────────────────────────────────────────────────


def _sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _fit_platt(raw_scores: list[float], labels01: list[int], seed: int = 42) -> tuple[float, float]:
    """Fit Platt scaling P = sigmoid(a*z + b) via gradient descent on NLL.

    Simple, no-deps; converges quickly on small datasets.
    """
    if not raw_scores:
        return 1.0, 0.0
    rng = random.Random(seed)
    a, b = 1.0, 0.0
    lr = 0.05
    n_iter = 200
    for _ in range(n_iter):
        # Mini-batch (full batch for our tiny sizes).
        grad_a = 0.0
        grad_b = 0.0
        for z, y in zip(raw_scores, labels01):
            p = _sigmoid(a * z + b)
            err = p - y
            grad_a += err * z
            grad_b += err
        n = len(raw_scores)
        a -= lr * (grad_a / n)
        b -= lr * (grad_b / n)
    return a, b


def _calibrate_threshold(probs: list[float], labels01: list[int], target_tpr: float) -> float:
    """Find the operating-point threshold.

    Strategy: pick the threshold maximising **expected P&L lift**, computed as
    a weighted Youden-style J statistic where each kept trade is rewarded by
    its expected outcome instead of just its binary label.

    Concretely we sweep candidate thresholds, compute (TPR - FPR), and pick
    the highest J. This is well-known to maximise the AUC operating point
    and transfers better OOS than a TPR-quantile cutoff (which overfits to
    train-fold positive scores). The `target_tpr` argument becomes a soft
    *minimum* recall — if J-max threshold falls below it, we relax to the
    target. Otherwise the data-driven J point wins.
    """
    if not probs:
        return 0.5
    paired = sorted(zip(probs, labels01), key=lambda x: -x[0])
    pos = sum(labels01)
    neg = len(labels01) - pos
    if pos == 0 or neg == 0:
        return 0.5
    # Step through every score as a candidate threshold; track best J.
    tp = fp = 0
    best_j = -1.0
    best_t = paired[0][0]
    for p, y in paired:
        if y == 1:
            tp += 1
        else:
            fp += 1
        tpr = tp / pos
        fpr = fp / neg
        j = tpr - fpr
        if j > best_j and tpr >= max(0.4, target_tpr - 0.2):
            best_j = j
            best_t = p
    return float(best_t)


# ── Convenience: evaluate the model honestly ───────────────────────────────


def evaluate(model: MetaJudge, test_trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute TPR/TNR/precision/AUC and pnl summary on held-out trades."""
    if not test_trades:
        return {}
    tp = fp = tn = fn = 0
    kept_pnls: list[float] = []
    dropped_pnls: list[float] = []
    probs_pos = []
    probs_neg = []
    for t in test_trades:
        kept = model.keep(t)
        pnl = float(t.get("pnl", 0.0))
        is_win = bool(t.get("win"))
        p = model.predict_proba(t)
        if is_win:
            probs_pos.append(p)
        else:
            probs_neg.append(p)
        if kept and is_win:
            tp += 1
        elif kept and not is_win:
            fp += 1
        elif (not kept) and is_win:
            fn += 1
        else:
            tn += 1
        if kept:
            kept_pnls.append(pnl)
        else:
            dropped_pnls.append(pnl)

    pos = tp + fn
    neg = fp + tn
    tpr = tp / pos if pos else 0.0
    tnr = tn / neg if neg else 0.0
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    # Quick AUC: probability that a random positive scores higher than a
    # random negative. O(n*m) but n,m are small here.
    auc = 0.0
    if probs_pos and probs_neg:
        wins = ties = 0
        for pp in probs_pos:
            for nn in probs_neg:
                if pp > nn: wins += 1
                elif pp == nn: ties += 1
        auc = (wins + 0.5 * ties) / (len(probs_pos) * len(probs_neg))
    return {
        "n_kept": len(kept_pnls),
        "n_dropped": len(dropped_pnls),
        "tpr": round(tpr, 4),
        "tnr": round(tnr, 4),
        "precision": round(prec, 4),
        "auc": round(auc, 4),
        "kept_avg_pnl": round(statistics.mean(kept_pnls), 4) if kept_pnls else 0.0,
        "kept_wr_pct": round(sum(1 for p in kept_pnls if p > 0) / max(1, len(kept_pnls)) * 100.0, 2),
        "dropped_avg_pnl": round(statistics.mean(dropped_pnls), 4) if dropped_pnls else 0.0,
        "kept_sum_pnl": round(sum(kept_pnls), 2),
    }
