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
  // New bullish detectors with documented academic edge.
  pead: "Post-Earnings Drift",
  quality_breakout: "Quality Breakout",
  unusual_options_activity: "Unusual Options Activity",
  quality_value_52w_low: "Quality Value (52w Low)",
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
  // PEAD drift plays out over ~30-60 sessions → Long-term.
  pead: "Long-term",
  // Quality Breakout is fundamentally-gated → multi-week hold by design.
  quality_breakout: "Long-term",
  // Unusual options flow is typically a short-fuse signal.
  unusual_options_activity: "Swing",
  // Module A: QV + 52w-low, 180-day hold by design → Long-term.
  quality_value_52w_low: "Long-term",
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
 * Timeframe is a property of the signal TYPE — each detector encodes its
 * own natural horizon (a gap is an intraday/next-day event; a chart
 * pattern plays out over a swing; a quality/value setup is a multi-month
 * hold). Conviction is a SEPARATE axis, already surfaced as `strength` on
 * the card, so we deliberately do NOT let strength move a signal between
 * timeframe tabs.
 *
 * History: an earlier version promoted high-strength signals to longer
 * horizons. In practice the detectors saturate near the top of the
 * strength scale (gaps fire 7–10, double_top fires 10), so the promotion
 * thresholds sat below the firing floor and promoted ~everything —
 * emptying the Intraday tab and making the mapping non-deterministic.
 * Removed in favour of this pure type → timeframe lookup. The `strength`
 * argument is retained for call-site stability and a possible future
 * "horizon badge" that annotates conviction without re-bucketing.
 */
export function getSignalTimeframe(signalType: string, _strength?: number): "Intraday" | "Swing" | "Long-term" {
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
