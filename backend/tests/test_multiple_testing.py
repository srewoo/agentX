from __future__ import annotations
"""A3 — overfitting guardrails.

The key property under test: random-noise combos (win rate ≈ break-even) must
NOT be promoted, even when many are tested at once, while a genuinely strong,
well-sampled combo is. This is what stops the autonomous loop from overfitting.
"""
from app.services.multiple_testing import (
    Candidate,
    benjamini_hochberg,
    binomial_sf_pvalue,
    passed_keys,
    select_significant,
)


def test_binomial_pvalue_strong_edge_is_small():
    # 70/100 vs a 50% null is highly significant.
    assert binomial_sf_pvalue(70, 100, 0.5) < 0.001
    # 50/100 vs 50% null is not significant at all.
    assert binomial_sf_pvalue(50, 100, 0.5) > 0.3


def test_binomial_pvalue_large_n_uses_normal_approx():
    # Just exercises the n>1000 branch without error and returns a sane prob.
    p = binomial_sf_pvalue(1100, 2000, 0.5)
    assert 0.0 <= p <= 1.0
    assert p < 0.001  # 55% on 2000 trades is significant


def test_benjamini_hochberg_basic_control():
    # One tiny p-value among large ones → only it is kept.
    pvals = [0.001, 0.4, 0.6, 0.8, 0.9]
    keep = benjamini_hochberg(pvals, alpha=0.05)
    assert keep[0] is True
    assert not any(keep[1:])


def test_benjamini_hochberg_all_null_keeps_nothing():
    pvals = [0.4, 0.5, 0.6, 0.7, 0.8]
    assert benjamini_hochberg(pvals, alpha=0.05) == [False] * 5


def test_noise_combos_are_not_promoted():
    # 20 combos each ~50% win rate on 100 trades — pure noise. With FDR
    # control, essentially none should be promoted (the whole point of A3).
    import random
    rng = random.Random(42)
    cands = [
        Candidate(key=f"noise{i}", wins=rng.randint(44, 56), n=100)
        for i in range(20)
    ]
    verdicts = select_significant(cands, p0=0.5, alpha=0.05, min_trades=30)
    assert passed_keys(verdicts) == []


def test_strong_combo_passes_among_noise():
    # A genuinely strong, well-sampled edge (72% on 200 trades, p << BH
    # threshold) clears FDR control even surrounded by 19 noise combos.
    cands = [Candidate(f"noise{i}", wins=50, n=100) for i in range(19)]
    cands.append(Candidate("real_edge", wins=144, n=200))
    verdicts = select_significant(cands, p0=0.5, alpha=0.05, min_trades=30)
    assert "real_edge" in passed_keys(verdicts)
    assert all(k == "real_edge" for k in passed_keys(verdicts))


def test_small_sample_blocked_even_if_high_winrate():
    # 9/10 looks great but is below the min-sample floor → not promotable.
    cands = [Candidate("tiny", wins=9, n=10)]
    verdicts = select_significant(cands, min_trades=30)
    assert passed_keys(verdicts) == []
    assert "insufficient sample" in verdicts[0].reason


def test_winrate_below_null_blocked():
    cands = [Candidate("losing", wins=40, n=200)]
    verdicts = select_significant(cands, p0=0.5, min_trades=30)
    assert passed_keys(verdicts) == []


def test_empty_input_is_safe_default():
    assert select_significant([]) == []
