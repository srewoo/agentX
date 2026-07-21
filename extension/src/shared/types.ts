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
  llm_verdict: "keep" | "drop" | "downgrade" | null;
  llm_reason: string | null;
  // Bull/Bear/Judge debate verdict — populated only when debate_enabled
  // and the signal made the top-N debated cohort.
  debate_winner?: "bull" | "bear" | "inconclusive" | null;
  debate_synthesis?: string | null;
  debate_confidence?: number | null;
  // Multi-perspective specialist analyst output — populated only when
  // multi_perspective_enabled and the signal made the top-N cohort.
  mp_aggregate_score?: number | null;
  mp_consensus?: "strong_confirm" | "confirm" | "mixed" | "contradict" | "strong_contradict" | null;
  mp_synthesis?: string | null;
  /** JSON string of per-perspective records: {perspective, score, confidence, summary}. */
  mp_perspectives_json?: string | null;
  exchange?: "NSE" | "BSE";
  current_price: number | null;
  metadata: Record<string, unknown>;
  created_at: string; // ISO timestamp
  read: boolean;
  dismissed: boolean;
}

export interface StockQuote {
  symbol: string;
  exchange?: "NSE" | "BSE";
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
  atr?: number | null;
  atr_pct?: number | null;
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

export interface DeepSignalAnalysis {
  verdict: "ACT" | "WATCH" | "AVOID" | "EXIT_REVIEW";
  confidence: number;
  summary: string;
  bull_case: string[];
  bear_case: string[];
  invalidations: string[];
  portfolio_note: string;
  risk_controls: string[];
  data_gaps: string[];
  not_advice: string;
  engine: string;
  symbol: string;
  reasoning_effort: string | null;
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

  // ── Broker integration (real-time quotes + option Greeks) ─────────
  /** Which broker to use for live data. "" = fall back to yfinance. */
  broker?: "" | "angelone" | "kite";

  // AngelOne SmartAPI credentials (all four required to authenticate).
  angelone_api_key?: string;
  angelone_client_code?: string;
  angelone_mpin?: string;
  angelone_totp_secret?: string;

  // Kite Connect credentials. access_token is refreshed ~daily.
  kite_api_key?: string;
  kite_api_secret?: string;
  kite_access_token?: string;
  // Upstox data source — daily OAuth access token (write-only; the backend
  // never echoes it back, only an `upstox_access_token_configured` flag).
  upstox_access_token?: string;
  upstox_access_token_configured?: boolean;
  upstox_api_key?: string;
  upstox_api_key_configured?: boolean;
  upstox_api_secret?: string;
  upstox_api_secret_configured?: boolean;
  // Twelve Data keyed fallback.
  twelvedata_api_key?: string;
  // Financial Modeling Prep (fundamentals + earnings calendar) + Finnhub (macro / USD-INR).
  // Write-only: the backend returns only a `<key>_configured` boolean, never the value.
  fmp_api_key?: string;
  fmp_api_key_configured?: boolean;
  finnhub_api_key?: string;
  finnhub_api_key_configured?: boolean;
  // ── added by Tier 1/2/3 buildout ─────────────────────────────────
  onboarding_complete?: boolean;
  theme?: "dark" | "light";
  audio_alerts?: boolean;
  audio_strength_threshold?: number; // 1-10
  muted_symbols?: string[];
  muted_signal_types?: string[];
  snoozed_until?: string | null; // ISO timestamp; signals suppressed until then
  telegram_bot_token?: string;
  telegram_chat_id?: string;
  telegram_min_strength?: number;
  encrypt_keys?: boolean; // when true, *_api_key fields are stored as encrypted blobs
  custom_screener_presets?: CustomScreenerPreset[];

  // ── Advisor mode (ATR-based risk + position sizing + regime + costs) ──
  /** Trading capital in INR — used for position-size suggestions on signals. */
  capital?: number;
  /** Risk per trade as % of capital (default 1.0 = 1% — Van Tharp / Kelly fraction). */
  risk_per_trade_pct?: number;
  /** ATR multiplier for stop-loss (default 1.5 — tighter for swing, wider for positional). */
  atr_sl_mult?: number;
  /** ATR multiplier for target (default 3.0 — gives 1:2 R:R when SL=1.5). */
  atr_target_mult?: number;
  /** When true (default), Dashboard hides regime-incompatible signals. */
  regime_filter?: boolean;
  /** Round-trip cost as % of trade value (brokerage + STT + slippage; default 0.5). */
  roundtrip_cost_pct?: number;
  /** Collapse signals on the same (symbol, day, direction) into one card (default true). */
  dedupe_signals?: boolean;

  /** Autonomous loop: auto-create paper trades from high-strength signals. Off by default. */
  auto_paper_trade?: boolean;
  /** Min signal strength to qualify for auto-paper-trade. */
  auto_paper_min_strength?: number;
  /** Cap on simultaneous open auto-paper positions (portfolio heat). */
  auto_paper_max_open?: number;

  /** Layer-2 LLM judge: when on, the orchestrator runs one batched LLM call
   *  per scan that endorses/downgrades/drops each deterministic candidate. */
  llm_judging_enabled?: boolean;

  /** Bull/Bear/Judge debate: when on, the top-3 strength-≥7 directional
   *  signals get stress-tested by three LLM agents (bull case, bear case,
   *  judge). The judge's winner flips the badge override the same way
   *  llm_verdict=drop does. Up to 9 extra LLM calls per scan — off by default. */
  debate_enabled?: boolean;

  /** Multi-perspective specialist analyst: 4 LLM agents (technical,
   *  fundamental, sentiment, macro) + synthesiser on top-5 signals. Most
   *  expensive layer — up to 25 LLM calls per scan. Surfaces a per-
   *  perspective contribution breakdown on the signal card. Off by default. */
  multi_perspective_enabled?: boolean;
}

export interface FundamentalsResponse {
  symbol: string;
  valuation?: {
    pe: number | null;
    forward_pe: number | null;
    pb: number | null;
    ps: number | null;
    ev_ebitda: number | null;
  };
  growth?: {
    revenue_growth: number | null;
    earnings_growth: number | null;
    quarterly_earnings_growth: number | null;
  };
  profitability?: {
    roe: number | null;
    roa: number | null;
    profit_margin: number | null;
    operating_margin: number | null;
    gross_margin: number | null;
  };
  financial_health?: {
    debt_to_equity: number | null;
    current_ratio: number | null;
    quick_ratio: number | null;
    total_debt: number | null;
    total_cash: number | null;
  };
  dividends?: {
    dividend_yield: number | null;
    dividend_rate: number | null;
    payout_ratio: number | null;
  };
  ownership?: {
    insider_pct: number | null;
    institutional_pct: number | null;
  };
  health_score?: number; // 0-10
  signal?: string; // "Strong Buy" | "Buy" | "Hold" | ...
  sector?: string;
  industry?: string;
  sector_medians?: {
    pe?: number | null;
    pb?: number | null;
    ev_ebitda?: number | null;
    roe?: number | null;
    profit_margin?: number | null;
    operating_margin?: number | null;
    debt_to_equity?: number | null;
    revenue_growth?: number | null;
    earnings_growth?: number | null;
    dividend_yield?: number | null;
  };
  error?: string;
}

export interface CustomScreenerPreset {
  id: string;
  name: string;
  params: ScreenerParams;
  created_at: string;
}

export interface ScreenerParams {
  rsi_min?: number;
  rsi_max?: number;
  volume_ratio_min?: number;
  change_pct_min?: number;
  change_pct_max?: number;
  market_cap_min?: number;
  market_cap_max?: number;
  sector?: string;
  limit?: number;
}

// ── New shared types for added features ─────────────────────────────
export interface NewsItem {
  title: string;
  url?: string;
  source?: string;
  published_at?: string;
  sentiment?: number; // -1..1
  symbols?: string[];
  summary?: string;
}

export interface CorporateAction {
  symbol: string;
  name?: string;
  action_type: string; // "Dividend" | "Split" | "Bonus" | "Earnings" | "AGM" etc.
  ex_date?: string;
  record_date?: string;
  details?: string;
}

export interface OptionsAnalysis {
  symbol: string;
  pcr?: number;
  max_pain?: number;
  unusual_oi?: Array<{ strike: number; type: "CE" | "PE"; change_oi: number; oi: number }>;
  sentiment?: string;
  expiry?: string;
  spot?: number;
  error?: string;
}

export interface BlockDeal {
  symbol: string;
  client?: string;
  qty?: number;
  price?: number;
  date?: string;
  side?: "buy" | "sell";
}

export type InsightSeverity = "warn" | "good" | "info";
export type InsightKind = "drift" | "wow" | "recommended_mutes";

export interface Insight {
  kind: InsightKind;
  severity: InsightSeverity;
  title: string;
  signal_type?: string;
  direction?: string;
  live_win_rate?: number;
  baseline_win_rate?: number;
  delta_pct?: number;
  sample_size?: number;
  signal_types?: string[];
  current?: { wr: number | null; pnl: number | null; best: string | null; worst: string | null };
  previous?: { wr: number | null; pnl: number | null };
  action?: "mute" | "apply_mutes" | null;
  action_label?: string | null;
}

export interface InsightsResponse {
  insights: Insight[];
  count: number;
}

/** Returned by `POST /api/scan/trigger` — the scan runs asynchronously, the
 *  client polls `/api/scan/status` until `status === "completed" | "failed"`. */
export interface ScanTriggerResponse {
  job_id: string;
  status: "running";
  started_at: string;
  already_running: boolean;
}

export interface ScanStatus {
  job_id: string | null;
  status: "idle" | "running" | "completed" | "failed";
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number | null;
  total_symbols: number;
  completed_symbols: number;
  current_symbol: string | null;
  progress_pct: number;        // 0–100
  signals_so_far: number;
  error: string | null;
}

export interface AutomationStatusHeartbeat {
  last_run_at: string;
  summary?: Record<string, unknown> | null;
}

export interface AutomationStatus {
  orchestrator_running: boolean;
  market_open: boolean;
  auto_paper_enabled: boolean;
  daily_backtest_enabled: boolean;
  open_positions: number;
  heartbeats: Record<string, AutomationStatusHeartbeat>;
  last_backtest_at: string | null;
  next_daily_backtest_utc: string;
  next_weekly_backtest_utc: string;
}

export interface BacktestRun {
  id: number;
  run_at: string;
  period: string;
  eval_window_days: number;
  stocks_count: number;
  total_signals: number;
  avg_pnl_pct: number | null;
  directional_win_rate: number | null;
  best_signal_type: string | null;
  worst_signal_type: string | null;
}

export interface PerformanceByTypeRow {
  signal_type: string;
  direction: string;
  timeframe?: string;
  total_signals: number;
  wins: number;
  losses?: number;
  win_rate: number;        // percent (0-100)
  avg_pnl_pct: number;     // percent
  updated_at?: string;
}

export interface SignalEdgeRow {
  signal_type: string;
  direction: "bullish" | "bearish";
  family: string;
  win_rate: number;
  avg_pnl: number;
  trades: number;
}

export interface SignalEdgeResponse {
  meta: {
    source: string;
    period: string;
    eval_window_days: number;
    stocks: number;
    total_signals: number;
    transaction_cost_pct: number;
  };
  recommended_mutes: string[];
  rows: SignalEdgeRow[];
}

export interface BacktestResult {
  symbol: string;
  total_signals: number;
  windows: Record<string, { win_rate: number; avg_pnl_pct: number; trades: number }>;
  by_signal_type?: Array<{ signal_type: string; trades: number; win_rate: number; avg_pnl_pct: number }>;
  methodology?: {
    transaction_cost_pct: number;
    walk_forward: boolean;
    entry: string;
    eval: string;
  };
}

export interface PaperTrade {
  id: string;
  symbol: string;
  side: "BUY" | "SELL";
  qty: number;
  entry_price: number;
  entry_at: string;
  signal_id?: string;
  /** Captured from the source signal so the backend can attribute outcomes
   *  to the right (signal_type, direction) bucket for edge tracking. */
  signal_type?: string;
  signal_strength?: number;
  signal_direction?: "bullish" | "bearish";
  target?: number;
  stop_loss?: number;
  status: "open" | "closed";
  exit_price?: number;
  exit_at?: string;
  notes?: string;
  /** Server-side trade_id once the auto-open is POSTed to the backend.
   *  Absence indicates "not yet synced" — periodic retry loop catches up. */
  backend_trade_id?: string;
  /** ISO timestamp of the most-recent successful sync (open or close). */
  backend_synced_at?: string;
}

export interface Holding {
  symbol: string;
  qty: number;
  avg_price: number;
  notes?: string;
}

export interface WatchlistGroup {
  symbol: string;
  group: string;
}

export interface HealthResponse {
  status: string;
  db: string;
  cache: string;
  last_scan: string | null;
  market_open: boolean;
  orchestrator_running: boolean;
}

export interface Scorecard {
  forward_trades: number;
  target_trades: number;
  progress_pct: number;
  excess_expectancy_pct: number | null;
  excess_expectancy_lb95_pct: number | null;
  raw_expectancy_pct: number | null;
  win_rate: number | null;
  win_rate_ci: [number, number] | null;
  sharpe_per_trade: number | null;
  max_drawdown_pct: number | null;
  brier: number | null;
  benchmark_symbol: string | null;
  attributed_trades: number | null;
  verdict: "PROVEN" | "SIGNIFICANT_BUT_UNDER_SAMPLE" | "PROMISING" | "NO_EDGE_YET";
  ready: boolean;
}
