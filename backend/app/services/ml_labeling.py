from __future__ import annotations
"""Triple-barrier labeling + purged K-fold cross-validation.

Implements two techniques from López de Prado, *Advances in Financial
Machine Learning* (Wiley 2018):

  • Triple-barrier method (Ch. 3) — label = first of [TP hit, SL hit,
    time expiry], not fixed Nd return. Aligns labels with how trades
    actually exit; reduces noise from intra-window mean reversion.

  • Purged K-fold (Ch. 7) — when a sample at time t is held in test
    set, every train sample whose *label window* overlaps t is purged
    (and an embargo extends past the test set to prevent leakage from
    auto-correlated features). Standard K-fold leaks; purged fixes it.

Pure Python + NumPy. Used by `ml_meta_label.py` for meta-labeling.
"""
from dataclasses import dataclass
from typing import Iterator, Optional, Sequence

import numpy as np


@dataclass(frozen=True)
class TripleBarrierLabel:
    """Outcome of a triple-barrier trade, per entry bar.

    Attributes:
        label:        +1 = TP hit, −1 = SL hit, 0 = time expired
        pnl_pct:      realised PnL in percent (signed by direction)
        bars_held:    bars from entry to exit
        exit_reason:  "tp" | "sl" | "time"
    """
    label: int
    pnl_pct: float
    bars_held: int
    exit_reason: str


def triple_barrier(
    closes: Sequence[float],
    entry_idx: int,
    *,
    direction: str,
    tp_pct: float,
    sl_pct: float,
    max_bars: int,
    highs: Optional[Sequence[float]] = None,
    lows: Optional[Sequence[float]] = None,
) -> Optional[TripleBarrierLabel]:
    """Compute the triple-barrier label for an entry at `entry_idx`.

    `tp_pct` and `sl_pct` are positive percentages (e.g. 2.0 for 2%).
    `direction` is "bullish" or "bearish". When intraday highs/lows
    are passed we use them — much more accurate than close-only.

    Returns None when entry_idx + max_bars exceeds the series.
    """
    n = len(closes)
    if entry_idx < 0 or entry_idx + 1 >= n:
        return None
    entry = float(closes[entry_idx])
    if entry <= 0:
        return None
    end = min(n, entry_idx + 1 + max_bars)
    if end - entry_idx - 1 < 1:
        return None

    direction_up = direction == "bullish"
    tp_price = entry * (1 + tp_pct / 100.0) if direction_up else entry * (1 - tp_pct / 100.0)
    sl_price = entry * (1 - sl_pct / 100.0) if direction_up else entry * (1 + sl_pct / 100.0)

    for i in range(entry_idx + 1, end):
        hi = float(highs[i]) if highs is not None else float(closes[i])
        lo = float(lows[i]) if lows is not None else float(closes[i])
        # When the bar's range straddles both barriers, the conservative
        # convention is "SL first" — the only assumption that doesn't
        # bias backtests upward.
        if direction_up:
            if lo <= sl_price:
                return TripleBarrierLabel(
                    label=-1, pnl_pct=-sl_pct, bars_held=i - entry_idx, exit_reason="sl",
                )
            if hi >= tp_price:
                return TripleBarrierLabel(
                    label=1, pnl_pct=tp_pct, bars_held=i - entry_idx, exit_reason="tp",
                )
        else:
            if hi >= sl_price:
                return TripleBarrierLabel(
                    label=-1, pnl_pct=-sl_pct, bars_held=i - entry_idx, exit_reason="sl",
                )
            if lo <= tp_price:
                return TripleBarrierLabel(
                    label=1, pnl_pct=tp_pct, bars_held=i - entry_idx, exit_reason="tp",
                )
    # Time barrier — use last close.
    exit_close = float(closes[end - 1])
    raw_pnl = (exit_close - entry) / entry * 100.0
    pnl = raw_pnl if direction_up else -raw_pnl
    return TripleBarrierLabel(
        label=0, pnl_pct=round(pnl, 4), bars_held=end - 1 - entry_idx, exit_reason="time",
    )


def purged_kfold_split(
    n_samples: int,
    label_horizons: Sequence[int],
    *,
    n_splits: int = 5,
    embargo_pct: float = 0.01,
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """Yield (train_idx, test_idx) pairs with López de Prado purging.

    Each sample `i` has a label window `[i, i + label_horizons[i]]`.
    When `i` is in the test fold, any train sample `j` whose label
    window overlaps `[i, i + horizons[i]]` is **purged** (removed) to
    prevent leakage. Additionally a forward embargo of
    `int(embargo_pct * n)` bars after the test fold is excluded from
    training — handles autocorrelated features.
    """
    if n_splits < 2:
        raise ValueError("n_splits must be >= 2")
    if len(label_horizons) != n_samples:
        raise ValueError("label_horizons length must match n_samples")

    indices = np.arange(n_samples)
    fold_size = n_samples // n_splits
    embargo = int(embargo_pct * n_samples)

    horizons = np.array(label_horizons, dtype=int)
    label_end = indices + horizons  # exclusive end of each label window

    for k in range(n_splits):
        test_start = k * fold_size
        test_end = (k + 1) * fold_size if k < n_splits - 1 else n_samples
        test_idx = indices[test_start:test_end]

        # Train candidates = everything else.
        candidate = np.ones(n_samples, dtype=bool)
        candidate[test_start:test_end] = False

        # Purge: drop any train sample whose label window touches the
        # test window OR whose start time is inside the embargo.
        for ti in test_idx:
            # Drop train rows j where label_end[j] >= ti (label still
            # active when test starts) AND j < ti (before the test row).
            mask_left = (indices < ti) & (label_end >= ti)
            candidate &= ~mask_left
        # Embargo on the right side.
        if embargo > 0:
            emb_end = min(n_samples, test_end + embargo)
            candidate[test_end:emb_end] = False

        train_idx = indices[candidate]
        if train_idx.size == 0 or test_idx.size == 0:
            continue
        yield train_idx, test_idx


def deflated_sharpe(
    sharpe: float,
    *,
    n_trials: int,
    n_samples: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """Bailey & López de Prado (2014) deflated Sharpe ratio.

    `n_trials` is how many strategies/parameter combos were tried
    before reporting this Sharpe — corrects for selection bias.
    Returns the probability that the true Sharpe is positive.
    """
    import math
    if n_samples < 4 or n_trials < 1:
        return 0.0
    # Expected maximum Sharpe under H0 (no skill, many trials).
    euler_mascheroni = 0.5772156649
    z = lambda p: math.sqrt(2) * _erfinv(2 * p - 1)
    expected_max = (1 - euler_mascheroni) * z(1 - 1 / n_trials) + \
        euler_mascheroni * z(1 - 1 / (n_trials * math.e))
    # Variance of observed Sharpe (Lo 2002).
    var = (1 - skew * sharpe + (kurtosis - 1) / 4 * sharpe ** 2) / (n_samples - 1)
    if var <= 0:
        return 0.0
    dsr_z = (sharpe - expected_max) / math.sqrt(var)
    # Convert z to probability via standard normal CDF.
    return _phi(dsr_z)


def _erfinv(x: float) -> float:
    import math
    # Approximation (Winitzki) — good to 5 decimals.
    a = 0.147
    sign = 1 if x >= 0 else -1
    ln_1mx2 = math.log(max(1e-12, 1 - x * x))
    first = 2 / (math.pi * a) + ln_1mx2 / 2
    return sign * math.sqrt(math.sqrt(first ** 2 - ln_1mx2 / a) - first)


def _phi(z: float) -> float:
    import math
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))
