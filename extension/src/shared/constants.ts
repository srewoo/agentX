export const DEFAULT_BACKEND_URL = "http://localhost:8020";
export const DEFAULT_ALARM_NAME = "stockpilot-scan";
export const MAX_STORED_SIGNALS = 100;

export const SIGNAL_TYPE_LABELS: Record<string, string> = {
  price_spike: "Price Spike",
  volume_spike: "Volume Spike",
  breakout: "Breakout",
  rsi_extreme: "RSI Extreme",
  macd_crossover: "MACD Crossover",
  sentiment_shift: "Sentiment Shift",
  price_alert: "Price Alert",
  double_bottom: "Double Bottom",
  double_top: "Double Top",
  head_and_shoulders: "Head & Shoulders",
  inverse_head_and_shoulders: "Inverse H&S",
  cup_and_handle: "Cup & Handle",
  narrow_range: "Narrow Range (NR7)",
  consolidation_breakout: "Consolidation Breakout",
  inside_day: "Inside Day",
  bullish_engulfing: "Bullish Engulfing",
  bearish_engulfing: "Bearish Engulfing",
  morning_star: "Morning Star",
  evening_star: "Evening Star",
  hammer: "Hammer",
  shooting_star: "Shooting Star",
  ema_crossover: "EMA Crossover",
  "52_week_high": "52-Week High",
  "52_week_low": "52-Week Low",
  gap_up: "Gap Up",
  gap_down: "Gap Down",
  volume_dry_up: "Volume Dry-Up",
  rsi_divergence: "RSI Divergence",
  macd_divergence: "MACD Divergence",
  confluence: "Multi-Signal Confluence",
  options_flow: "Options Flow",
};

// Derived timeframe for each signal type
export const SIGNAL_TIMEFRAME: Record<string, "Intraday" | "Swing" | "Long-term"> = {
  price_spike: "Intraday",
  volume_spike: "Intraday",
  rsi_extreme: "Swing",
  macd_crossover: "Swing",
  breakout: "Swing",
  sentiment_shift: "Swing",
  price_alert: "Intraday",
  double_bottom: "Swing",
  double_top: "Swing",
  head_and_shoulders: "Swing",
  inverse_head_and_shoulders: "Swing",
  cup_and_handle: "Long-term",
  narrow_range: "Intraday",
  consolidation_breakout: "Swing",
  inside_day: "Intraday",
  bullish_engulfing: "Intraday",
  bearish_engulfing: "Intraday",
  morning_star: "Swing",
  evening_star: "Swing",
  hammer: "Intraday",
  shooting_star: "Intraday",
  ema_crossover: "Swing",
  "52_week_high": "Long-term",
  "52_week_low": "Long-term",
  gap_up: "Intraday",
  gap_down: "Intraday",
  volume_dry_up: "Swing",
  rsi_divergence: "Swing",
  macd_divergence: "Swing",
  confluence: "Swing",
  options_flow: "Swing",
};

// Derive action label from direction
export const DIRECTION_ACTION: Record<string, string> = {
  bullish: "BUY",
  bearish: "SELL",
  neutral: "HOLD",
};

export const ACTION_COLORS: Record<string, string> = {
  BUY: "#10B981",
  SELL: "#EF4444",
  HOLD: "#F59E0B",
};

export const DIRECTION_COLORS: Record<string, string> = {
  bullish: "#10B981",
  bearish: "#EF4444",
  neutral: "#F59E0B",
};

/**
 * Resolve the display timeframe for a signal.
 *
 * Beyond the static type → timeframe table we promote high-conviction
 * patterns to longer horizons, because the same chart pattern at
 * strength 9 implies a multi-week setup, not the 1–5 day "Swing" hold
 * its base type suggests. Without this, the Long-term tab was almost
 * always empty (only cup_and_handle / 52w extremes ever landed there).
 *
 * Promotion rules:
 *   - High-strength multi-bar reversal/continuation patterns → Long-term
 *   - High-strength swing patterns → still Swing (no demotion to Intraday)
 *   - Single-day candle patterns never promote past Swing
 */
const _LONG_TERM_PROMOTION_AT_STRENGTH_8: ReadonlySet<string> = new Set([
  "breakout",
  "head_and_shoulders",
  "inverse_head_and_shoulders",
  "double_top",
  "double_bottom",
  "consolidation_breakout",
]);

const _SWING_PROMOTION_AT_STRENGTH_7: ReadonlySet<string> = new Set([
  // Intraday candle patterns escalate to Swing on a strong reading —
  // they tend to play out over 2–4 sessions when conviction is high.
  "bullish_engulfing",
  "bearish_engulfing",
  "hammer",
  "shooting_star",
  "gap_up",
  "gap_down",
]);

export function getSignalTimeframe(signalType: string, strength: number): "Intraday" | "Swing" | "Long-term" {
  // Legacy override kept for explicit breakout/strength=7 coverage.
  if (signalType === "breakout" && strength >= 7) return "Long-term";

  if (strength >= 8 && _LONG_TERM_PROMOTION_AT_STRENGTH_8.has(signalType)) {
    return "Long-term";
  }
  if (strength >= 7 && _SWING_PROMOTION_AT_STRENGTH_7.has(signalType)) {
    return "Swing";
  }
  return SIGNAL_TIMEFRAME[signalType] ?? "Swing";
}

// Latest as of 2026-Q2. First entry is the default for fresh installs;
// flagship models are listed first within each provider.
export const LLM_MODELS: Record<string, string[]> = {
  gemini: [
    "gemini-3.1-flash",  // default — fast + cheap
    "gemini-3.1-pro",    // flagship
    "gemini-3-flash",    // fallback
    "gemini-2.5-pro",    // legacy
  ],
  openai: [
    "gpt-5-mini",        // default — fast + cheap
    "gpt-5",             // flagship
    "gpt-5-nano",        // cheapest
    "gpt-4.1",           // legacy fallback
    "gpt-4.1-mini",      // legacy fallback
    "o4-mini",           // reasoning, budget
    "o3",                // reasoning legacy
  ],
  claude: [
    "claude-sonnet-4-5",            // default — balanced
    "claude-opus-4-7",              // flagship
    "claude-haiku-4-5-20251001",    // fast + cheap
    "claude-sonnet-4-6",            // legacy fallback
  ],
};
