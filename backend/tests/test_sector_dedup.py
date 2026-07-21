from __future__ import annotations
"""Regression: sector-concentration dedup must not collapse unmapped symbols.

Before the fix every symbol without a sector mapping fell into one shared
"Unknown" bucket, so the concentration cap (_MAX_SECTOR_POSITIONS) kept only 2
unrelated names for the ENTIRE scan and silently starved the Live feed.
"""
from app.services.orchestrator import _dedup_by_sector


def _sig(sym, strength=5, st="volume_spike"):
    return {"symbol": sym, "strength": strength, "signal_type": st}


def test_unmapped_symbols_are_not_collapsed():
    # 5 symbols, none in the lookup → each is its own bucket → all survive.
    sigs = [_sig(f"SYM{i}") for i in range(5)]
    kept = _dedup_by_sector(sigs, sector_lookup={}, max_positions=2)
    assert len(kept) == 5


def test_known_sector_is_capped():
    # 4 banking names share a bucket → only strongest 2 survive.
    lookup = {s: "Banking" for s in ("HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK")}
    sigs = [_sig(s) for s in lookup]
    kept = _dedup_by_sector(sigs, lookup, max_positions=2)
    assert len(kept) == 2
    assert all(k["symbol"] in lookup for k in kept)


def test_mixed_mapped_and_unmapped():
    # 3 Banking (capped to 2) + 3 unmapped (each own bucket, all kept) = 5.
    lookup = {"HDFCBANK": "Banking", "ICICIBANK": "Banking", "SBIN": "Banking"}
    sigs = [_sig("HDFCBANK"), _sig("ICICIBANK"), _sig("SBIN"),
            _sig("SMALLCAP1"), _sig("SMALLCAP2"), _sig("SMALLCAP3")]
    kept = _dedup_by_sector(sigs, lookup, max_positions=2)
    assert len(kept) == 5
    banking = [k for k in kept if lookup.get(k["symbol"]) == "Banking"]
    assert len(banking) == 2


def test_explicit_unknown_string_is_not_collapsed():
    # Symbols explicitly mapped to "Unknown" also get per-symbol buckets.
    lookup = {"A": "Unknown", "B": "Unknown", "C": "Unknown"}
    sigs = [_sig("A"), _sig("B"), _sig("C")]
    kept = _dedup_by_sector(sigs, lookup, max_positions=2)
    assert len(kept) == 3
