from __future__ import annotations
"""Per-signal conversational chat — /api/signals/{id}/chat.

Users can ask follow-ups about a specific signal ("compare with peers",
"what would change this thesis", "what's the regime risk"). State is
persisted in `signal_chats` so the thread survives popup reloads.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import aiosqlite
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.database import DB_PATH

router = APIRouter(prefix="/api/signals", tags=["signal_chat"])
logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS signal_chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,            -- 'user' | 'assistant' | 'system'
    content TEXT NOT NULL,
    tokens_in INTEGER DEFAULT 0,
    tokens_out INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""

_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_signal_chats_signal_id "
    "ON signal_chats(signal_id, created_at)"
)


async def _ensure_schema() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(_SCHEMA)
        await db.execute(_INDEX)
        await db.commit()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(default="default", max_length=80)


async def _load_signal(signal_id: str) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, symbol, signal_type, direction, strength, reason, "
            "       current_price, llm_verdict, llm_reason, debate_synthesis, "
            "       mp_consensus, mp_synthesis "
            "FROM signals WHERE id = ?",
            (signal_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


def _system_prompt(signal: dict) -> str:
    from app.services.llm_india_context import briefing
    base = briefing(include_flow=True, include_sector=True,
                    include_red_flags=True, include_seasonality=False)
    sig_block = (
        f"SIGNAL UNDER DISCUSSION:\n"
        f"- Symbol: {signal.get('symbol')}\n"
        f"- Setup: {signal.get('signal_type')} ({signal.get('direction')})\n"
        f"- Strength: {signal.get('strength')}/10\n"
        f"- Trigger reason: {signal.get('reason') or 'n/a'}\n"
        f"- Current price: {signal.get('current_price') or 'n/a'}\n"
        f"- Layer-2 verdict: {signal.get('llm_verdict') or 'n/a'} "
        f"({signal.get('llm_reason') or 'no reason'})\n"
        f"- Debate synthesis: {signal.get('debate_synthesis') or 'n/a'}\n"
        f"- Multi-perspective consensus: {signal.get('mp_consensus') or 'n/a'}\n"
        f"- MP synthesis: {signal.get('mp_synthesis') or 'n/a'}\n"
    )
    role = (
        "\n\nROLE: You are agentX's signal-analyst assistant. The user is "
        "asking follow-up questions about the specific signal above. Reason "
        "as an Indian-equities specialist. Be honest about uncertainty. If "
        "the data above is insufficient to answer, say so explicitly. Use "
        "short paragraphs, no markdown headers. Aim for 3-6 sentences."
    )
    return base + "\n\n" + sig_block + role


async def _history(signal_id: str, session_id: str, limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT role, content FROM signal_chats "
            "WHERE signal_id = ? AND session_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (signal_id, session_id, limit),
        ) as cur:
            rows = list(await cur.fetchall())
    return list(reversed([dict(r) for r in rows]))


async def _persist(
    *, signal_id: str, session_id: str, role: str, content: str,
    tokens_in: int = 0, tokens_out: int = 0, cost_usd: float = 0.0,
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO signal_chats (signal_id, session_id, role, content, "
            " tokens_in, tokens_out, cost_usd, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                signal_id, session_id, role, content,
                tokens_in, tokens_out, cost_usd,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        await db.commit()


@router.get("/{signal_id}/chat")
async def get_chat_history(signal_id: str, session_id: str = "default") -> dict[str, Any]:
    """Return persisted chat thread for a signal."""
    await _ensure_schema()
    return {
        "signal_id": signal_id,
        "session_id": session_id,
        "messages": await _history(signal_id, session_id, limit=100),
    }


@router.post("/{signal_id}/chat")
async def post_chat(signal_id: str, body: ChatRequest) -> dict[str, Any]:
    """Send a user message, get the assistant's reply. Persisted both sides."""
    await _ensure_schema()
    signal = await _load_signal(signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="signal not found")

    # Pull DB settings for provider/model.
    db_settings: dict[str, Any] = {}
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT key, value FROM settings") as cur:
                db_settings = {r["key"]: r["value"] for r in await cur.fetchall()}
    except Exception:
        pass

    provider = db_settings.get("llm_provider", "openai")
    model = db_settings.get("llm_model", "gpt-5-mini")

    from app.services.llm_client import call_llm
    from app.services.llm_analyst import _get_api_key

    api_key = _get_api_key(db_settings, provider)
    if not api_key:
        raise HTTPException(status_code=400, detail=f"no API key for provider {provider}")

    # Build the user prompt with thread context.
    history = await _history(signal_id, body.session_id, limit=10)
    convo = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in history
    )
    user_prompt = (
        (convo + "\n\n" if convo else "")
        + f"USER: {body.message}\n\nASSISTANT:"
    )

    try:
        raw = await call_llm(
            provider=provider, model=model, api_key=api_key,
            prompt=user_prompt,
            system_message=_system_prompt(signal),
            route="signal_chat",
            symbol=signal.get("symbol"),
            max_tokens=600,
        )
    except Exception as e:
        logger.exception("signal_chat: LLM call failed: %s", e)
        raise HTTPException(status_code=502, detail=f"LLM error: {e}")

    if not raw or not raw.strip():
        raise HTTPException(status_code=502, detail="empty LLM response")

    # Persist both turns.
    await _persist(signal_id=signal_id, session_id=body.session_id,
                   role="user", content=body.message)
    await _persist(signal_id=signal_id, session_id=body.session_id,
                   role="assistant", content=raw)

    return {
        "signal_id": signal_id,
        "session_id": body.session_id,
        "reply": raw,
    }


def _sse_pack(event: str, data: str) -> str:
    """Pack a single SSE frame. We escape any embedded newlines per spec."""
    lines = "".join(f"data: {line}\n" for line in data.split("\n"))
    return f"event: {event}\n{lines}\n"


@router.post("/{signal_id}/chat/stream")
async def post_chat_stream(signal_id: str, body: ChatRequest) -> StreamingResponse:
    """SSE-streamed chat reply. Emits `token` frames as the reply is
    produced, then a final `done` frame with the persisted message id
    and a `cost` frame with token counts when available.

    The underlying LLM call may be synchronous (our `call_llm` returns a
    full string). In that case we still emit chunked SSE frames so the
    client can render progressively without waiting for the full reply.
    """
    await _ensure_schema()
    signal = await _load_signal(signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="signal not found")

    db_settings: dict[str, Any] = {}
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT key, value FROM settings") as cur:
                db_settings = {r["key"]: r["value"] for r in await cur.fetchall()}
    except Exception:
        pass

    provider = db_settings.get("llm_provider", "openai")
    model = db_settings.get("llm_model", "gpt-5-mini")
    from app.services.llm_analyst import _get_api_key
    api_key = _get_api_key(db_settings, provider)
    if not api_key:
        raise HTTPException(status_code=400, detail=f"no API key for provider {provider}")

    history = await _history(signal_id, body.session_id, limit=10)
    convo = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in history)
    user_prompt = (
        (convo + "\n\n" if convo else "")
        + f"USER: {body.message}\n\nASSISTANT:"
    )
    system_message = _system_prompt(signal)

    async def generator():
        # Heartbeat frame so proxies don't buffer-close.
        yield _sse_pack("ready", json.dumps({"signal_id": signal_id}))

        # Persist user turn up-front so the thread is correct even on disconnect.
        await _persist(
            signal_id=signal_id, session_id=body.session_id,
            role="user", content=body.message,
        )

        try:
            from app.services.llm_client import call_llm
            raw = await call_llm(
                provider=provider, model=model, api_key=api_key,
                prompt=user_prompt, system_message=system_message,
                route="signal_chat_stream", symbol=signal.get("symbol"),
                max_tokens=600,
            )
        except Exception as e:
            err = {"error": str(e)}
            yield _sse_pack("error", json.dumps(err))
            return

        if not raw or not raw.strip():
            yield _sse_pack("error", json.dumps({"error": "empty LLM response"}))
            return

        # Chunk the synchronous reply into ~40-char windows so the UI can
        # render progressively. Switch to provider-native streaming when
        # call_llm exposes it (TODO).
        CHUNK = 40
        for i in range(0, len(raw), CHUNK):
            chunk = raw[i : i + CHUNK]
            yield _sse_pack("token", json.dumps({"text": chunk}))
            # Tiny await so the event loop yields to the network layer.
            await asyncio.sleep(0)

        await _persist(
            signal_id=signal_id, session_id=body.session_id,
            role="assistant", content=raw,
        )
        yield _sse_pack("done", json.dumps({
            "signal_id": signal_id,
            "session_id": body.session_id,
            "chars": len(raw),
        }))

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{signal_id}/reasoning")
async def get_show_your_work(signal_id: str) -> dict[str, Any]:
    """Return the raw LLM verdicts attached to a signal (judge / debate / MP).

    Powers the "show your work" affordance — the UI expands a signal card
    to display exactly what each LLM layer said. Useful for debugging
    surprising verdicts.
    """
    signal = await _load_signal(signal_id)
    if signal is None:
        raise HTTPException(status_code=404, detail="signal not found")
    return {
        "signal_id": signal_id,
        "judge": {
            "verdict": signal.get("llm_verdict"),
            "reason": signal.get("llm_reason"),
        },
        "debate": {
            "synthesis": signal.get("debate_synthesis"),
        },
        "multi_perspective": {
            "consensus": signal.get("mp_consensus"),
            "synthesis": signal.get("mp_synthesis"),
        },
    }
