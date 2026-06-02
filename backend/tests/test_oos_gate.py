from __future__ import annotations
"""Tests for app.services.oos_gate — the out-of-sample shipping gate."""
import json

import pytest

from app.services.oos_gate import (
    aggregate_oos,
    evaluate_oos_gate,
    load_latest_walk_forward,
    latest_verdict,
)


def _sym(trades, wr, pnl, mc_p5, lb, horizon="5d"):
    return {"oos_summary": {
        "trades": trades,
        f"win_rate_{horizon}": wr,
        f"avg_pnl_{horizon}": pnl,
        f"mc_wr_p5_{horizon}": mc_p5,
        f"win_rate_lb95_{horizon}": lb,
    }}


class TestAggregate:
    def test_trade_weighted(self):
        # 100 trades @ 60% and 300 @ 40% → weighted 45%.
        results = [_sym(100, 60.0, 1.0, 50.0, 55.0), _sym(300, 40.0, -1.0, 30.0, 35.0)]
        agg = aggregate_oos(results)
        assert agg["total_trades"] == 400
        assert agg["win_rate"] == pytest.approx(45.0)
        # avg pnl weighted: (100*1 + 300*-1)/400 = -0.5
        assert agg["avg_pnl_pct"] == pytest.approx(-0.5)

    def test_skips_zero_trade_symbols(self):
        results = [_sym(0, 99.0, 9.0, 99.0, 99.0), _sym(50, 50.0, 0.5, 48.0, 46.0)]
        agg = aggregate_oos(results)
        assert agg["total_trades"] == 50
        assert agg["symbols"] == 1


class TestGate:
    def test_pass_when_all_bars_clear(self):
        results = [_sym(120, 52.0, 0.8, 49.0, 48.0)]
        g = evaluate_oos_gate(results)
        assert g["verdict"] == "PASS"
        assert g["shippable"] is True

    def test_fail_on_negative_expectancy(self):
        results = [_sym(500, 48.0, -0.3, 46.0, 47.0)]
        g = evaluate_oos_gate(results)
        assert g["verdict"] == "FAIL"
        assert g["shippable"] is False
        assert any("negative expectancy" in r for r in g["reasons"])

    def test_review_when_positive_but_fragile(self):
        # +EV but MC p5 below the fragility line and thin sample.
        results = [_sym(40, 51.0, 0.2, 38.0, 44.0)]
        g = evaluate_oos_gate(results)
        assert g["verdict"] == "REVIEW"
        assert g["shippable"] is False
        assert any("fragile" in r for r in g["reasons"])
        assert any("insufficient evidence" in r for r in g["reasons"])

    def test_empty_results_fail(self):
        g = evaluate_oos_gate([])
        assert g["verdict"] == "FAIL"
        assert "no out-of-sample data" in g["reasons"][0]


class TestLoader:
    def test_loads_latest_and_evaluates(self, tmp_path):
        f = tmp_path / "walk_fwd_20260101_000000.json"
        f.write_text(json.dumps([_sym(150, 55.0, 0.6, 50.0, 49.0)]))
        results, path = load_latest_walk_forward(tmp_path)
        assert results is not None and path.endswith(".json")
        v = latest_verdict(tmp_path)
        assert v["verdict"] == "PASS"
        assert v["source"].endswith(".json")

    def test_unknown_when_no_file(self, tmp_path):
        v = latest_verdict(tmp_path)
        assert v["verdict"] == "UNKNOWN"
        assert v["shippable"] is False

    def test_normalises_single_dict(self, tmp_path):
        f = tmp_path / "walk_fwd_x.json"
        f.write_text(json.dumps(_sym(120, 52.0, 0.5, 48.0, 47.0)))
        results, _ = load_latest_walk_forward(tmp_path)
        assert isinstance(results, list) and len(results) == 1
