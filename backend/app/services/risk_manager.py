from __future__ import annotations
"""
Risk management utilities for agentX paper trading and live signal scoring.

Provides ATR-based position sizing, portfolio heat calculation, and
risk-adjusted stop-loss computation.
"""
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Default risk constants
_DEFAULT_RISK_PER_TRADE_PCT = 1.0   # Risk 1% of capital per trade (ATR method)
_DEFAULT_ATR_MULTIPLIER = 2.0        # Stop = entry ± (ATR × 2)
_MAX_POSITION_PCT = 5.0             # Never more than 5% of capital in one position
_MAX_PORTFOLIO_HEAT_PCT = 6.0       # Total open risk as % of capital


def calculate_position_size(
    capital: float,
    entry_price: float,
    atr: float,
    risk_per_trade_pct: float = _DEFAULT_RISK_PER_TRADE_PCT,
    atr_multiplier: float = _DEFAULT_ATR_MULTIPLIER,
    direction: str = "bullish",
) -> dict[str, Any]:
    """Calculate ATR-based position size and stop-loss.

    Logic:
      stop_distance = atr * atr_multiplier
      risk_amount = capital * (risk_per_trade_pct / 100)
      shares = int(risk_amount / stop_distance)
      cap at max_position = capital * MAX_POSITION_PCT%

    Returns:
        {
            "shares": int,
            "stop_loss": float,
            "position_value": float,
            "risk_amount": float,
            "stop_distance": float,
        }
    """
    if entry_price <= 0 or atr <= 0:
        return {"shares": 0, "stop_loss": 0.0, "position_value": 0.0, "risk_amount": 0.0, "stop_distance": 0.0}

    stop_distance = atr * atr_multiplier
    risk_amount = capital * (risk_per_trade_pct / 100.0)
    shares = int(risk_amount / stop_distance)

    # Cap at max position size
    max_position = capital * (_MAX_POSITION_PCT / 100.0)
    if shares * entry_price > max_position:
        shares = int(max_position / entry_price)

    if shares <= 0:
        return {"shares": 0, "stop_loss": 0.0, "position_value": 0.0, "risk_amount": 0.0, "stop_distance": stop_distance}

    position_value = round(shares * entry_price, 2)

    if direction == "bullish":
        stop_loss = round(entry_price - stop_distance, 2)
    else:
        stop_loss = round(entry_price + stop_distance, 2)

    return {
        "shares": shares,
        "stop_loss": stop_loss,
        "position_value": position_value,
        "risk_amount": round(shares * stop_distance, 2),
        "stop_distance": round(stop_distance, 2),
    }


def calculate_portfolio_heat(open_trades: list[dict]) -> dict[str, Any]:
    """Calculate total portfolio heat from open positions.

    heat_per_trade = shares * abs(entry_price - stop_loss)
    total_heat = sum of all heat_per_trade

    Returns:
        {
            "total_heat": float,      # Rs. amount at risk across all positions
            "heat_pct": float,        # as % of capital (estimated from positions)
            "positions": list[dict],  # per-position heat breakdown
        }
    """
    positions = []
    total_heat = 0.0

    for trade in open_trades:
        try:
            entry = float(trade.get("entry_price", 0))
            stop = float(trade.get("stop_loss", 0))
            shares = int(trade.get("shares", 0))
            if entry > 0 and stop > 0 and shares > 0:
                heat = shares * abs(entry - stop)
                total_heat += heat
                positions.append({
                    "symbol": trade.get("symbol", ""),
                    "heat": round(heat, 2),
                })
        except (ValueError, TypeError):
            continue

    return {
        "total_heat": round(total_heat, 2),
        "positions": positions,
    }


def update_trailing_stop(
    entry_price: float,
    current_stop: float,
    current_price: float,
    direction: str,
) -> float:
    """Update trailing stop as trade moves in favour.

    Rules (bullish):
    - If price moved +1.5% from entry → lock in breakeven (stop = entry)
    - If price moved +3%+ → trail stop to 3% below current price

    Bearish is symmetrical.

    Returns updated stop_loss price. Never moves stop against the trade.
    """
    if entry_price <= 0:
        return current_stop

    if direction == "bullish":
        move_pct = (current_price - entry_price) / entry_price * 100
        if move_pct >= 3.0:
            new_stop = round(current_price * 0.97, 2)
            return max(current_stop, new_stop)
        elif move_pct >= 1.5:
            return max(current_stop, entry_price)
        return current_stop

    elif direction == "bearish":
        move_pct = (entry_price - current_price) / entry_price * 100
        if move_pct >= 3.0:
            new_stop = round(current_price * 1.03, 2)
            return min(current_stop, new_stop)
        elif move_pct >= 1.5:
            return min(current_stop, entry_price)
        return current_stop

    return current_stop
