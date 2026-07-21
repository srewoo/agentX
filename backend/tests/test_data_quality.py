from __future__ import annotations
"""3.3 — cross-source reconciliation + data-quality ledger."""
import os
import sqlite3
import tempfile

import pandas as pd
import pytest

from app.services import data_quality as dq


# ── pure detectors ──
def test_compare_sources_flags_disagreement():
    # 100 vs 101 = 1% spread > 0.5% floor → disagreement.
    d = dq.compare_sources({"upstox": 100.0, "nse": 101.0, "yfinance": 100.2})
    assert d is not None
    assert d["kind"] == "source_disagreement"
    assert d["value"] == pytest.approx(1.0, abs=1e-6)


def test_compare_sources_within_tolerance_is_clean():
    assert dq.compare_sources({"upstox": 100.0, "nse": 100.3}) is None   # 0.3% < 0.5%
    assert dq.compare_sources({"upstox": 100.0}) is None                 # need ≥2
    assert dq.compare_sources({"a": 0, "b": 0}) is None                  # no valid prices


def test_detect_jumps_flags_missed_corporate_action():
    # A 50% one-day drop looks like an unadjusted split.
    close = [100, 100, 50, 51]
    df = pd.DataFrame({"Close": close}, index=pd.date_range("2026-01-01", periods=4))
    jumps = dq.detect_jumps(df, "INFY")
    assert len(jumps) == 1
    assert jumps[0]["kind"] == "suspicious_jump"
    assert jumps[0]["value"] >= 45.0


def test_detect_jumps_ignores_normal_moves():
    df = pd.DataFrame({"Close": [100, 102, 101, 103]},
                      index=pd.date_range("2026-01-01", periods=4))
    assert dq.detect_jumps(df, "INFY") == []


# ── ledger ──
@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    os.unlink(path)


@pytest.mark.asyncio
async def test_record_and_summarize_issues(db):
    n = await dq.record_issues([
        {"symbol": "INFY", "kind": "suspicious_jump", "value": 50.0, "detail": "x"},
        {"symbol": "TCS", "kind": "source_disagreement", "value": 1.2,
         "detail": "y", "sources": {"nse": 100, "upstox": 101.2}},
    ], db_path=db)
    assert n == 2
    summary = await dq.health_summary(db_path=db)
    assert summary["status"] == "issues_present"
    assert summary["total_issues"] == 2
    assert summary["by_kind"]["suspicious_jump"]["count"] == 1
    recent = await dq.recent_issues(db_path=db)
    assert len(recent) == 2


@pytest.mark.asyncio
async def test_clean_ledger_summary(db):
    summary = await dq.health_summary(db_path=db)
    assert summary["status"] == "clean"
    assert summary["total_issues"] == 0
