from __future__ import annotations
"""Live macro snapshot — today's NIFTY / FII / DII / VIX / USDINR / Brent.

Used by every LLM layer so reasoning is grounded in *today's* numbers,
not abstract rules. Cached at 15-min granularity to keep token cost and
upstream rate limits flat across a scan.

The snapshot is fire-and-forget for the caller; on upstream failure we
return the last-known cache (if any) with a `stale=True` flag, or a
neutral empty snapshot. Never raises — LLM prompts must still build.
"""
import asyncio
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 15-minute cache bucket — single key per quarter-hour. We deliberately
# keep this in-process; macro inputs are cheap to recompute on cold-start
# and a per-process cache is sufficient at our scan cadence.
_CACHE: dict[str, "MarketSnapshot"] = {}
_CACHE_LOCK = asyncio.Lock()
_CACHE_MAX_ENTRIES = 32


def _bucket_key(now: Optional[datetime] = None) -> str:
    n = now or datetime.now(timezone.utc)
    minute_bucket = (n.minute // 15) * 15
    return n.strftime(f"%Y-%m-%dT%H:{minute_bucket:02d}")


@dataclass
class MarketSnapshot:
    as_of: str
    nifty_close: Optional[float] = None
    nifty_pct: Optional[float] = None
    bank_nifty_close: Optional[float] = None
    bank_nifty_pct: Optional[float] = None
    india_vix: Optional[float] = None
    usd_inr: Optional[float] = None
    brent_usd: Optional[float] = None
    fii_net_cr: Optional[float] = None
    dii_net_cr: Optional[float] = None
    sector_rotation: Optional[str] = None
    sector_movers: list[dict[str, Any]] = field(default_factory=list)
    stale: bool = False
    errors: list[str] = field(default_factory=list)

    def to_briefing_block(self) -> str:
        """Render the 6-line LIVE-MARKET block prepended to LLM prompts.

        Numbers only — no narration. The LLM already has the playbook
        for what each number means; this just calls today's play.
        """
        def fmt(v: Optional[float], *, pct: bool = False, dp: int = 1) -> str:
            if v is None:
                return "n/a"
            if pct:
                sign = "+" if v >= 0 else ""
                return f"{sign}{v:.{dp}f}%"
            return f"{v:.{dp}f}"

        lines = [
            f"LIVE MARKET (as of {self.as_of}):",
            (
                f"- NIFTY 50: {fmt(self.nifty_close)} ({fmt(self.nifty_pct, pct=True)})"
                f"  |  BANKNIFTY: {fmt(self.bank_nifty_close)} ({fmt(self.bank_nifty_pct, pct=True)})"
            ),
            (
                f"- India VIX: {fmt(self.india_vix)}  |  USD/INR: {fmt(self.usd_inr, dp=2)}"
                f"  |  Brent: ${fmt(self.brent_usd, dp=2)}"
            ),
            (
                f"- FII net: ₹{fmt(self.fii_net_cr, dp=0)} Cr  |  DII net: ₹{fmt(self.dii_net_cr, dp=0)} Cr"
                + ("  (prev session)" if self.fii_net_cr is not None else "")
            ),
        ]
        if self.sector_rotation:
            lines.append(f"- Rotation: {self.sector_rotation}")
        if self.stale:
            lines.append("- (snapshot is stale — upstream feed missed; reason in errors)")
        return "\n".join(lines)


# ── upstream fetchers (best-effort, never raise) ────────────────────────


async def _safe_last_close(symbol: str, period: str = "5d") -> tuple[Optional[float], Optional[float]]:
    """Return (last_close, pct_change_vs_prev). None on any failure."""
    try:
        from app.services.data_fetcher import async_fetch_history
        df = await async_fetch_history(symbol, period=period, interval="1d")
        if df is None or df.empty or "Close" not in df.columns:
            return None, None
        closes = df["Close"].dropna()
        if len(closes) < 2:
            last = float(closes.iloc[-1]) if len(closes) else None
            return last, None
        last = float(closes.iloc[-1])
        prev = float(closes.iloc[-2])
        pct = round((last - prev) / prev * 100.0, 2) if prev else None
        return round(last, 2), pct
    except Exception as e:
        logger.debug("market_snapshot.%s fetch failed: %s", symbol, e)
        return None, None


async def _fetch_fii_dii() -> tuple[Optional[float], Optional[float]]:
    try:
        from app.services.fii_dii import get_fii_dii_data
        data = await get_fii_dii_data()
        if not data:
            return None, None
        fii = data.get("fii_net") if isinstance(data.get("fii_net"), (int, float)) else None
        dii = data.get("dii_net") if isinstance(data.get("dii_net"), (int, float)) else None
        return fii, dii
    except Exception as e:
        logger.debug("market_snapshot fii_dii failed: %s", e)
        return None, None


async def _fetch_vix() -> Optional[float]:
    try:
        from app.services.market_data import get_india_vix
        return await get_india_vix()
    except Exception as e:
        logger.debug("market_snapshot vix failed: %s", e)
        return None


async def _fetch_usd_inr() -> Optional[float]:
    """USD/INR: Finnhub first (real-time, well-maintained), yfinance fallback."""
    try:
        from app.services.finnhub_fetcher import get_usd_inr
        rate = await get_usd_inr()
        if rate is not None:
            return rate
    except Exception as e:
        logger.debug("market_snapshot finnhub usd_inr failed: %s", e)
    last, _ = await _safe_last_close("INR=X", period="5d")
    return last


async def _compute_sector_rotation() -> tuple[Optional[str], list[dict[str, Any]]]:
    """Rank top 3 NSE sector indices by 5-day change for a rotation hint."""
    sectors = {
        "IT": "^CNXIT",
        "Bank": "^NSEBANK",
        "Auto": "^CNXAUTO",
        "FMCG": "^CNXFMCG",
        "Pharma": "^CNXPHARMA",
        "Metal": "^CNXMETAL",
    }
    movers: list[dict[str, Any]] = []
    try:
        from app.services.data_fetcher import async_fetch_history

        async def one(name: str, sym: str) -> Optional[dict[str, Any]]:
            try:
                df = await async_fetch_history(sym, period="10d", interval="1d")
                if df is None or df.empty or "Close" not in df.columns:
                    return None
                closes = df["Close"].dropna()
                if len(closes) < 5:
                    return None
                last = float(closes.iloc[-1])
                ref = float(closes.iloc[-5])
                pct5d = round((last - ref) / ref * 100.0, 2) if ref else 0.0
                return {"sector": name, "pct5d": pct5d}
            except Exception:
                return None

        results = await asyncio.gather(*(one(n, s) for n, s in sectors.items()))
        movers = [r for r in results if r is not None]
        movers.sort(key=lambda r: r["pct5d"], reverse=True)
        if len(movers) >= 2:
            top, bot = movers[0], movers[-1]
            spread = round(top["pct5d"] - bot["pct5d"], 2)
            rot = f"{top['sector']} leading {bot['sector']} by {spread}% W/W"
            return rot, movers[:3]
        return None, movers[:3]
    except Exception as e:
        logger.debug("market_snapshot sector rotation failed: %s", e)
        return None, movers


# ── public API ──────────────────────────────────────────────────────────


async def get_market_snapshot(force_refresh: bool = False) -> MarketSnapshot:
    """Return today's market snapshot, cached at 15-min granularity.

    Never raises. On total upstream failure, returns an empty snapshot
    with `stale=True` and the error list populated.
    """
    key = _bucket_key()
    if not force_refresh:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached

    async with _CACHE_LOCK:
        # Re-check after grabbing the lock — another concurrent call may
        # have already populated this bucket.
        if not force_refresh and key in _CACHE:
            return _CACHE[key]

        as_of = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
        errors: list[str] = []

        nifty_t, bn_t, vix, fii_dii, rotation_t = await asyncio.gather(
            _safe_last_close("^NSEI"),
            _safe_last_close("^NSEBANK"),
            _fetch_vix(),
            _fetch_fii_dii(),
            _compute_sector_rotation(),
            return_exceptions=False,
        )
        usd_inr = await _fetch_usd_inr()
        brent_t = await _safe_last_close("BZ=F", period="5d")

        snapshot = MarketSnapshot(
            as_of=as_of,
            nifty_close=nifty_t[0],
            nifty_pct=nifty_t[1],
            bank_nifty_close=bn_t[0],
            bank_nifty_pct=bn_t[1],
            india_vix=vix,
            usd_inr=usd_inr,
            brent_usd=brent_t[0],
            fii_net_cr=fii_dii[0],
            dii_net_cr=fii_dii[1],
            sector_rotation=rotation_t[0],
            sector_movers=rotation_t[1],
        )

        # Heuristic staleness flag: if every primary field is None, the
        # whole feed is down and we don't want to mislead the LLM.
        primary = [snapshot.nifty_close, snapshot.india_vix, snapshot.usd_inr]
        if all(p is None for p in primary):
            snapshot.stale = True
            errors.append("all primary feeds returned None")

        snapshot.errors = errors

        if len(_CACHE) >= _CACHE_MAX_ENTRIES:
            # Drop oldest bucket to keep memory bounded.
            oldest = sorted(_CACHE.keys())[0]
            _CACHE.pop(oldest, None)
        _CACHE[key] = snapshot

    return snapshot


async def get_live_briefing_block() -> str:
    """Convenience wrapper used by llm_india_context.briefing()."""
    snap = await get_market_snapshot()
    return snap.to_briefing_block()


def snapshot_to_dict(snap: MarketSnapshot) -> dict[str, Any]:
    return asdict(snap)
