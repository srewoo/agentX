from __future__ import annotations
"""A3 — overfitting guardrails for autonomous gating.

Before A1/A2 are allowed to *derive* promotions, mutes, and blocklists from the
walk-forward, this module is the statistical gate that stops the autonomous
loop from overfitting faster than a human ever could. When you test dozens of
(signal, direction[, regime]) combinations and keep the ones that "look
profitable", a handful will clear any naive threshold by chance alone — that is
exactly how the earlier in-sample "ceiling" turned out to be a +25pp fiction
(see backtest_results/CEILING_ANALYSIS.md).

Two defences, plus a default:

  1. **Per-combo significance.** Turn each combo's win record into a one-sided
     binomial p-value against a break-even null (p0 ≈ cost-adjusted 0.5): is
     this win rate *significantly* above break-even, or just noise?
  2. **Benjamini–Hochberg FDR control.** Across all combos tested in a run,
     control the false-discovery rate so the *expected* fraction of spurious
     promotions stays ≤ alpha. This is the multiple-testing correction the
     naive "WLB > 0" rule lacks.
  3. **Minimum sample + positive point estimate.** A combo must also clear a
     minimum trade count and actually have a win rate above the null — FDR
     significance on 12 trades is still not tradeable.

The default is **do nothing**: `select_significant` returns an empty list when
nothing clears, so a quiet week promotes nothing rather than reaching for the
least-insignificant noise.
"""
import math
from dataclasses import dataclass
from typing import Iterable, Optional


def binomial_sf_pvalue(wins: int, n: int, p0: float = 0.5) -> float:
    """One-sided p-value: P(X >= wins) under Binomial(n, p0). Lower ⇒ stronger.

    Exact for modest n via the survival sum; falls back to a normal
    approximation with continuity correction for large n (where the exact sum
    is slow). Returns 1.0 for empty/degenerate input (no evidence).
    """
    if n <= 0:
        return 1.0
    wins = max(0, min(n, int(wins)))
    p0 = min(max(p0, 1e-9), 1 - 1e-9)
    if n <= 1000:
        # Exact: sum_{k=wins}^{n} C(n,k) p0^k (1-p0)^(n-k)
        total = 0.0
        for k in range(wins, n + 1):
            total += math.comb(n, k) * (p0 ** k) * ((1 - p0) ** (n - k))
        return min(1.0, max(0.0, total))
    # Normal approximation with continuity correction.
    mean = n * p0
    sd = math.sqrt(n * p0 * (1 - p0))
    if sd == 0:
        return 1.0
    z = (wins - 0.5 - mean) / sd
    return 0.5 * math.erfc(z / math.sqrt(2))


def benjamini_hochberg(pvalues: list[float], alpha: float = 0.05) -> list[bool]:
    """Benjamini–Hochberg FDR control. Returns a keep-mask aligned to input.

    Controls the expected false-discovery rate at ``alpha`` across the family
    of tests. A hypothesis i is rejected (kept) if its p-value is below the
    largest BH threshold rank·alpha/m it clears.
    """
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    keep = [False] * m
    max_rank = -1
    for rank, idx in enumerate(order, start=1):
        if pvalues[idx] <= (rank / m) * alpha:
            max_rank = rank
    if max_rank >= 0:
        for rank, idx in enumerate(order, start=1):
            if rank <= max_rank:
                keep[idx] = True
    return keep


def wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> float:
    """Wilson score lower bound of a win-rate proportion (0.0 for empty n).

    The conservative end of the confidence interval — used by the per-signal
    kill rule so a combo dies only when even the *optimistic* case for it is
    weak, not on a single unlucky sample.
    """
    if n <= 0:
        return 0.0
    p = max(0.0, min(1.0, wins / n))
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


@dataclass(frozen=True)
class Candidate:
    """A combo's win record put forward for promotion.

    ``wins``/``n`` are the (typically backtest / walk-forward) record the FDR
    gate scores. ``live_wins``/``live_n`` are the FORWARD (paper/live) record,
    used by the per-signal kill rule and to justify overturning an irreversible
    demotion — backtest passes alone can't re-promote a demoted combo.
    """
    key: str
    wins: int
    n: int
    live_wins: int = 0
    live_n: int = 0


@dataclass(frozen=True)
class Verdict:
    key: str
    wins: int
    n: int
    win_rate: float
    p_value: float
    passed: bool
    reason: str


def select_significant(
    candidates: Iterable[Candidate],
    *,
    p0: float = 0.5,
    alpha: float = 0.05,
    min_trades: int = 30,
) -> list[Verdict]:
    """Filter candidates to those that survive all guardrails.

    A candidate passes only if it (a) has ≥ ``min_trades``, (b) has a win rate
    strictly above the ``p0`` null, and (c) clears Benjamini–Hochberg FDR
    control at ``alpha`` across the whole family. Returns Verdicts for ALL
    candidates (passed flag + reason) so the caller can log why each was kept
    or dropped; the empty-pass case (nothing clears) is the safe default.
    """
    cands = list(candidates)
    if not cands:
        return []
    pvals = [binomial_sf_pvalue(c.wins, c.n, p0) for c in cands]
    bh_keep = benjamini_hochberg(pvals, alpha)

    verdicts: list[Verdict] = []
    for c, p, fdr_ok in zip(cands, pvals, bh_keep):
        wr = (c.wins / c.n) if c.n > 0 else 0.0
        if c.n < min_trades:
            passed, reason = False, f"insufficient sample (n={c.n} < {min_trades})"
        elif wr <= p0:
            passed, reason = False, f"win rate {wr:.3f} not above null {p0:.3f}"
        elif not fdr_ok:
            passed, reason = False, f"fails FDR control (p={p:.4f}, alpha={alpha})"
        else:
            passed, reason = True, f"significant (p={p:.4f}, wr={wr:.3f}, n={c.n})"
        verdicts.append(Verdict(c.key, c.wins, c.n, round(wr, 4), round(p, 6), passed, reason))
    return verdicts


def passed_keys(verdicts: list[Verdict]) -> list[str]:
    """Convenience: just the keys that survived (the promotion set)."""
    return [v.key for v in verdicts if v.passed]
