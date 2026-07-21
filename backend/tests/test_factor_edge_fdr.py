from __future__ import annotations
"""Unit tests for the factor-edge significance p-value feeding the FDR gate.

The gate in recommendation_calibration._persist_run only persists factor edges
that survive Benjamini-Hochberg across the whole family. These tests pin the
per-edge p-value's behaviour: thin/degenerate buckets can never be promoted,
and a genuine mean shift on a well-sampled bucket is detectable.
"""
from app.services.recommendation_calibration import _edge_pvalue, _MIN_EDGE_SAMPLES
from app.services.multiple_testing import benjamini_hochberg


def test_thin_bucket_is_never_significant():
    # Fewer than the minimum aligned samples → p == 1.0.
    vals = [5.0] * (_MIN_EDGE_SAMPLES - 1)
    assert _edge_pvalue(vals, overall_avg=0.0) == 1.0


def test_zero_variance_is_not_significant():
    # Degenerate (all identical) → guarded to 1.0 rather than infinite t.
    vals = [2.0] * (_MIN_EDGE_SAMPLES + 10)
    assert _edge_pvalue(vals, overall_avg=0.0) == 1.0


def test_no_edge_gives_high_pvalue():
    # Aligned mean ≈ overall mean → not significant.
    vals = [0.1, -0.1] * 20  # mean 0, overall 0
    p = _edge_pvalue(vals, overall_avg=0.0)
    assert p > 0.10


def test_strong_positive_edge_is_significant():
    # Tight distribution centred well above the overall mean → low p-value.
    vals = [3.0, 3.2, 2.8, 3.1, 2.9] * 8  # n=40, mean ≈ 3.0, small variance
    p = _edge_pvalue(vals, overall_avg=0.0)
    assert p < 0.01


def test_fdr_keeps_only_real_edges_in_a_family():
    """A family of one genuine edge among many null edges: BH keeps the real
    one and rejects the noise."""
    real = [3.0, 3.2, 2.8, 3.1, 2.9] * 8
    nulls = [[0.1, -0.1] * 20 for _ in range(9)]
    pvals = [_edge_pvalue(real, 0.0)] + [_edge_pvalue(v, 0.0) for v in nulls]
    keep = benjamini_hochberg(pvals, alpha=0.10)
    assert keep[0] is True
    assert sum(keep) == 1  # only the genuine edge survives
