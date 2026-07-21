from __future__ import annotations
"""4.1 — calibration drift detection across weeks."""
import os
import sqlite3
import tempfile

import pytest

from app.database import CREATE_SETTINGS_TABLE
from app.services import calibration_monitor as cm


# ── pure detectors ──
def test_bin_miscalibrated_flags_overconfident_bin():
    bins = [
        {"bin": "50-60", "n": 20, "predicted": 0.58, "realized": 0.44},  # over-confident
        {"bin": "60-70", "n": 20, "predicted": 0.65, "realized": 0.66},  # fine
        {"bin": "70-80", "n": 3, "predicted": 0.75, "realized": 0.10},   # too few n
    ]
    off = cm.bin_miscalibrated(bins)
    assert len(off) == 1 and off[0]["bin"] == "50-60"


def test_is_drifting_requires_consecutive_weeks():
    assert cm.is_drifting([True]) is False              # need 2
    assert cm.is_drifting([True, False]) is False        # last week fine
    assert cm.is_drifting([False, True, True]) is True   # last 2 both bad
    assert cm.is_drifting([True, True, False]) is False


# ── DB history + verdict ──
@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute(CREATE_SETTINGS_TABLE)
    con.commit(); con.close()
    yield path
    os.unlink(path)


def _report(predicted, realized, n=20):
    return {"status": "ok", "samples": 200, "brier_calibrated": 0.24,
            "reliability": [{"bin": "50-60", "n": n, "predicted": predicted, "realized": realized}]}


@pytest.mark.asyncio
async def test_two_bad_weeks_trip_drift_alert(db):
    v1 = await cm.record_and_check(_report(0.58, 0.44), db_path=db)
    assert v1["miscalibrated_this_week"] is True
    assert v1["drifting"] is False          # only one bad week so far
    v2 = await cm.record_and_check(_report(0.59, 0.45), db_path=db)
    assert v2["drifting"] is True           # two consecutive → alert


@pytest.mark.asyncio
async def test_good_week_resets_drift(db):
    await cm.record_and_check(_report(0.58, 0.44), db_path=db)   # bad
    await cm.record_and_check(_report(0.58, 0.61), db_path=db)   # good → resets
    v3 = await cm.record_and_check(_report(0.58, 0.44), db_path=db)  # bad again
    assert v3["drifting"] is False          # not two-in-a-row


@pytest.mark.asyncio
async def test_insufficient_data_report_no_drift(db):
    v = await cm.record_and_check({"status": "insufficient_data"}, db_path=db)
    assert v["drifting"] is False
