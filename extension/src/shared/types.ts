export type SignalType =
  | "price_spike"
  | "volume_spike"
  | "breakout"
  | "rsi_extreme"
  | "macd_crossover"
  | "sentiment_shift"
  | "price_alert"
  | "double_bottom"
  | "double_top"
  | "head_and_shoulders"
  | "inverse_head_and_shoulders"
  | "cup_and_handle"
  | "narrow_range"
  | "consolidation_breakout"
  | "inside_day"
  | "bullish_engulfing"
  | "bearish_engulfing"
  | "morning_star"
  | "evening_star"
  | "hammer"
  | "shooting_star"
  | "ema_crossover"
  | "52_week_high"
  | "52_week_low"
  | "gap_up"
  | "gap_down"
  | "volume_dry_up"
  | "rsi_divergence"
  | "macd_divergence"
  | "confluence"
  | "options_flow";

export type Direction = "bullish" | "bearish" | "neutral";

export interface Signal {
  id: string;
  symbol: string;
  signal_type: SignalType;
  direction: Direction;
  strength: number; // 1-10
  reason: string;
  risk: string | null;
  llm_summary: string | null;
  current_price: number | null;
  metadata: Record<string, unknown>;
  created_at: string; // ISO timestamp
  read: boolean;
  dismissed: boolean;
}

export interface StockQuote {
  symbol: string;
  price: number | null;
  change: number | null;
  change_pct: number | null;
  volume: number | null;
  high: number | null;
  low: number | null;
  open: number | null;
  prev_close: number | null;
  name: string | null;
  market_cap: number | null;
}

export interface TechnicalsResponse {
  symbol: string;
  rsi: number | null;
  rsi_signal: string | null;
  adx: number | null;
  macd: {
    macd_line: number | null;
    signal_line: number | null;
    histogram: number | null;
    signal: string;
  } | null;
  moving_averages: {
    sma20: number | null;
    sma50: number | null;
    sma200: number | null;
    ema20: number | null;
  } | null;
  bollinger_bands: {
    upper: number | null;
    middle: number | null;
    lower: number | null;
    signal: string;
  } | null;
  support_resistance: {
    pivot: number | null;
    resistance: { r1: number | null; r2: number | null; r3: number | null };
    support: { s1: number | null; s2: number | null; s3: number | null };
  } | null;
  market_regime: {
    regime: string;
    confidence: number;
    description: string;
  } | null;
}

export interface AIAnalysis {
  stance: "BUY" | "SELL" | "HOLD" | "CAUTIOUS_BUY" | "CAUTIOUS_SELL";
  confidence: number;
  summary: string;
  key_reasons: string[];
  risks: string[];
  technical_outlook: string;
  sentiment: "Bullish" | "Bearish" | "Neutral";
  support_zone: string;
  resistance_zone: string;
}

export interface AIAnalysisResponse {
  symbol: string;
  name: string;
  timeframe: string;
  current_price: number | null;
  analysis: AIAnalysis;
}

export interface WatchlistItem {
  symbol: string;
  name: string;
  exchange: string;
  added_at: string;
}

export interface AppSettings {
  alert_interval_minutes: string;
  risk_mode: "conservative" | "balanced" | "aggressive";
  signal_types: string[];
  llm_provider: "gemini" | "openai" | "claude";
  llm_model: string;
  llm_api_key: string;
  openai_api_key: string;
  gemini_api_key: string;
  claude_api_key: string;
}

export interface HealthResponse {
  status: string;
  db: string;
  cache: string;
  last_scan: string | null;
  market_open: boolean;
  orchestrator_running: boolean;
}
