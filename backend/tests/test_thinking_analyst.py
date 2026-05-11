from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from unittest.mock import AsyncMock

import pytest

from app.database import CREATE_SETTINGS_TABLE, CREATE_SIGNALS_TABLE
from app.services import thinking_analyst as ta


@pytest.fixture
def thinking_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute(CREATE_SETTINGS_TABLE)
    con.execute(CREATE_SIGNALS_TABLE)
    con.execute("INSERT INTO settings (key, value) VALUES ('openai_api_key', 'sk-test')")
    con.execute("INSERT INTO settings (key, value) VALUES ('llm_provider', 'openai')")
    con.execute("INSERT INTO settings (key, value) VALUES ('llm_model', 'gpt-5')")
    con.execute(
        """INSERT INTO signals
           (id, symbol, signal_type, direction, strength, reason, risk,
            llm_summary, current_price, metadata, created_at, read, dismissed)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "sig-1",
            "RELIANCE",
            "gap_up",
            "bullish",
            8,
            "Gap up on volume",
            "Can reverse",
            None,
            100.0,
            json.dumps({"foo": "bar"}),
            "2026-05-11T10:00:00+00:00",
            0,
            0,
        ),
    )
    con.commit()
    con.close()
    monkeypatch.setattr(ta, "DB_PATH", path)
    monkeypatch.setattr(ta, "_decrypt_settings_map", lambda values: values)
    yield path
    os.unlink(path)


@pytest.mark.asyncio
async def test_analyze_signal_deep_uses_openai_responses(thinking_db, monkeypatch):
    monkeypatch.setattr(
        ta,
        "portfolio_recommendation_context",
        AsyncMock(return_value={"available": True, "notes": ["No concentration issue."]}),
    )
    monkeypatch.setattr(ta, "get_market_context", AsyncMock(return_value={"market_breadth": {"last": 1}}))
    fake_json = json.dumps(
        {
            "verdict": "WATCH",
            "confidence": 67,
            "summary": "Constructive but needs confirmation.",
            "bull_case": ["Positive gap edge."],
            "bear_case": ["Gap can fade."],
            "invalidations": ["Close below gap."],
            "portfolio_note": "No concentration issue.",
            "risk_controls": ["Use a stop."],
            "data_gaps": ["No live order book."],
            "not_advice": "Research signal only, not investment advice.",
        }
    )
    call = AsyncMock(return_value=fake_json)
    monkeypatch.setattr(ta, "call_openai_responses_json", call)

    result = await ta.analyze_signal_deep("sig-1", reasoning_effort="high")

    assert result["engine"] == "openai_responses"
    assert result["verdict"] == "WATCH"
    assert result["confidence"] == 67
    assert result["reasoning_effort"] == "high"
    assert call.call_args.kwargs["reasoning_effort"] == "high"
    assert call.call_args.kwargs["model"] == "gpt-5"
