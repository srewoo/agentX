from __future__ import annotations
"""
Canonical price-adjustment layer — ONE adjustment policy for every source.

Policy: **split/bonus-adjusted, dividends NOT adjusted** ("capital-adjusted").
This matches what the authenticated primary (Upstox) already serves, and what
Indian charting platforms display. Before this module existed the waterfall
silently mixed conventions:

  - Upstox                → split-adjusted by the provider
  - yfinance (default)    → split AND dividend adjusted  (auto_adjust=True)
  - NSE direct            → raw, completely unadjusted

so the same symbol could yield a different historical series depending on
which source won a given scan — corrupting technicals, backtests and ATR
stop levels around every ex-date. Every frame leaving ``data_fetcher`` now
passes through :func:`tag` / :func:`adjust_yfinance_frame` /
:func:`normalize_raw` and carries provenance in ``df.attrs``:

  df.attrs["px_source"]      — which source produced the frame
  df.attrs["px_adjustment"]  — "split_bonus" | "unknown" | "none_needed"

Fail-open: if split events can't be resolved for a raw frame we return the
frame unadjusted, tagged "unknown", and log a warning — visible, not silent.
"""
import asyncio
import logging
import time
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

POLICY = "split_bonus"

# Split events per symbol are cached so fallback-path fetches don't hammer
# yfinance. Positive results live for a day (splits are rare); failures are
# negative-cached briefly so an offline environment degrades quietly.
_SPLIT_TTL = 24 * 3600.0
_SPLIT_NEG_TTL = 1800.0
_split_cache: dict[str, tuple[float, list[tuple[pd.Timestamp, float]] | None]] = {}

_SPLIT_FETCH_TIMEOUT = 8.0  # seconds — metadata lookup; fail fast

# A one-day close-to-close move beyond this is almost certainly a missed
# corporate action (or bad tick), not a price move. Log it loudly.
_SUSPICIOUS_JUMP = 0.45


# ── Core back-adjustment math ─────────────────────────────────

def _back_adjust(df: pd.DataFrame, ratios: pd.Series) -> pd.DataFrame:
    """Back-adjust OHLC by per-bar split ratios.

    ``ratios[i]`` is the split factor that took effect ON bar i (1.0 = none).
    Bars strictly before a split divide prices by the ratio and multiply
    volume by it, so the series is continuous in post-split terms.
    """
    ratios = ratios.astype(float).where(ratios > 0, 1.0)
    # factor[i] = product of ratios for all bars AFTER i (own bar excluded —
    # the bar a split takes effect on already trades at post-split prices).
    factor = ratios[::-1].cumprod()[::-1] / ratios
    if (factor == 1.0).all():
        return df
    out = df.copy()
    for col in ("Open", "High", "Low", "Close"):
        if col in out.columns:
            out[col] = out[col] / factor
    if "Volume" in out.columns:
        out["Volume"] = out["Volume"] * factor
    return out


def adjust_yfinance_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Split-only adjust a raw yfinance frame (fetched with auto_adjust=False).

    Uses the ``Stock Splits`` action column returned in the same response —
    no extra network call. Dividends are deliberately NOT applied (POLICY).
    Action columns are dropped from the result.
    """
    if df.empty:
        return df
    if "Stock Splits" in df.columns:
        ratios = df["Stock Splits"].replace(0.0, 1.0)
        df = _back_adjust(df, ratios)
    df = df.drop(columns=[c for c in ("Dividends", "Stock Splits", "Capital Gains") if c in df.columns])
    tag(df, "yfinance", POLICY)
    return df


def apply_split_events(
    df: pd.DataFrame, events: list[tuple[pd.Timestamp, float]]
) -> pd.DataFrame:
    """Back-adjust a raw frame using explicit (effective_date, ratio) events."""
    if df.empty or not events:
        return df
    idx = pd.DatetimeIndex(pd.to_datetime(df.index)).tz_localize(None)
    ratios = pd.Series(1.0, index=range(len(df)))
    for when, ratio in events:
        when = pd.Timestamp(when).tz_localize(None) if pd.Timestamp(when).tzinfo else pd.Timestamp(when)
        if ratio <= 0:
            continue
        pos = idx.searchsorted(when)  # first bar at/after the effective date
        if 0 < pos < len(df):
            ratios.iloc[pos] *= ratio
        # pos == 0 → split predates the window (affects all bars equally:
        # nothing to do); pos == len → split is after the window: irrelevant.
    ratios.index = df.index
    return _back_adjust(df, ratios)


# ── Split-event source (cached, fail-open) ────────────────────

def _fetch_split_events_sync(symbol: str) -> list[tuple[pd.Timestamp, float]]:
    """Fetch split/bonus events via yfinance ticker metadata (sync)."""
    import yfinance as yf

    yf_sym = symbol if (symbol.startswith("^") or "." in symbol) else f"{symbol}.NS"
    splits = yf.Ticker(yf_sym).splits
    if splits is None or len(splits) == 0:
        return []
    return [(pd.Timestamp(ts), float(r)) for ts, r in splits.items() if float(r) > 0]


async def get_split_events(symbol: str) -> list[tuple[pd.Timestamp, float]] | None:
    """Cached split events for ``symbol``. ``None`` means "could not resolve".

    Never raises. Failures are negative-cached for a short window so scan
    loops don't repeatedly time out against a dead metadata source.
    """
    now = time.time()
    hit = _split_cache.get(symbol)
    if hit is not None:
        cached_at, events = hit
        ttl = _SPLIT_TTL if events is not None else _SPLIT_NEG_TTL
        if now - cached_at < ttl:
            return events
    try:
        loop = asyncio.get_event_loop()
        events = await asyncio.wait_for(
            loop.run_in_executor(None, _fetch_split_events_sync, symbol),
            timeout=_SPLIT_FETCH_TIMEOUT,
        )
    except Exception as e:
        logger.warning("price_adjuster: split lookup failed for %s: %s", symbol, e)
        events = None
    _split_cache[symbol] = (now, events)
    return events


def reset_split_cache() -> None:
    """Test hook."""
    _split_cache.clear()


# ── Normalization entry points ────────────────────────────────

def tag(df: pd.DataFrame, source: str, adjustment: str = POLICY) -> pd.DataFrame:
    """Stamp provenance attrs on a frame (in place; returns it for chaining)."""
    try:
        df.attrs["px_source"] = source
        df.attrs["px_adjustment"] = adjustment
    except Exception:  # attrs support is best-effort on exotic frames
        pass
    return df


async def normalize_raw(df: pd.DataFrame, symbol: str, source: str) -> pd.DataFrame:
    """Bring a RAW (unadjusted) frame onto the canonical policy.

    Used for NSE-direct raw data. If split events can't be resolved the frame
    is returned unadjusted and tagged "unknown".
    """
    if df.empty:
        return df
    events = await get_split_events(symbol)
    if events is None:
        logger.warning(
            "price_adjuster: %s frame for %s left UNADJUSTED (split events unavailable)",
            source, symbol,
        )
        return tag(df, source, "unknown")
    if events:
        df = apply_split_events(df, events)
    return tag(df, source, POLICY)


def flag_suspicious_jumps(df: pd.DataFrame, symbol: str) -> None:
    """Warn on residual one-day moves that look like a missed corporate action."""
    if df.empty or "Close" not in df.columns or len(df) < 2:
        return
    try:
        rets = df["Close"].pct_change().abs()
        bad = rets[rets > _SUSPICIOUS_JUMP]
        if not bad.empty:
            worst = bad.idxmax()
            logger.warning(
                "price_adjuster: %s has %d suspicious close-to-close jump(s) "
                "(worst %.0f%% on %s, source=%s) — possible unadjusted corporate action",
                symbol, len(bad), float(bad.max()) * 100, worst,
                df.attrs.get("px_source", "?"),
            )
    except Exception:
        pass
