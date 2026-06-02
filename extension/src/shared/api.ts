import { getBackendUrl, getSettings } from "./storage";
import type {
  Signal, StockQuote, TechnicalsResponse, AIAnalysisResponse, WatchlistItem, AppSettings, HealthResponse,
  NewsItem, CorporateAction, OptionsAnalysis, BlockDeal, BacktestResult, ScreenerParams, FundamentalsResponse,
  SignalEdgeResponse, InsightsResponse, BacktestRun, PerformanceByTypeRow, DeepSignalAnalysis,
  ScanTriggerResponse, ScanStatus, AutomationStatus,
} from "./types";

const DEFAULT_TIMEOUT_MS = 30_000; // 30 seconds

async function request<T>(path: string, options: RequestInit = {}, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<T> {
  const baseUrl = await getBackendUrl();
  const settings = await getSettings() as Record<string, string>;
  const apiKey = settings.api_key || "";

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(apiKey ? { "X-API-Key": apiKey } : {}),
  };

  // AbortController for request timeout
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetch(`${baseUrl}${path}`, {
      ...options,
      headers: { ...headers, ...(options.headers as Record<string, string> || {}) },
      signal: controller.signal,
    });

    if (!res.ok) {
      const error = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(error.detail || `HTTP ${res.status}`);
    }

    return res.json() as Promise<T>;
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new Error(`Request timed out after ${timeoutMs / 1000}s: ${path}`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

export const api = {
  health: () => request<HealthResponse>("/api/health"),

  // Signals
  getSignals: (since?: string, limit = 50) =>
    request<{ signals: Signal[]; unread_count: number }>(
      `/api/signals/latest${since ? `?since=${encodeURIComponent(since)}&limit=${limit}` : `?limit=${limit}`}`
    ),
  markRead: (id: string) => request<{ ok: boolean }>(`/api/signals/${id}/read`, { method: "POST" }),
  dismissSignal: (id: string) => request<{ ok: boolean }>(`/api/signals/${id}/dismiss`, { method: "POST" }),
  markAllRead: () => request<{ ok: boolean }>("/api/signals/read-all", { method: "POST" }),
  deepSignalAnalysis: (id: string, reasoningEffort: "low" | "medium" | "high" = "medium") =>
    request<{ data: DeepSignalAnalysis }>(
      `/api/signals/${encodeURIComponent(id)}/deep-analysis?reasoning_effort=${reasoningEffort}`,
      { method: "POST" },
      90_000,
    ),

  // Stocks
  search: (q: string) =>
    request<{ results: Array<{ symbol: string; name: string; exchange: string }> }>(`/api/stocks/search?q=${encodeURIComponent(q)}`),
  getQuote: (symbol: string, exchange: "NSE" | "BSE" = "NSE") =>
    request<StockQuote>(`/api/stocks/${symbol}/quote?exchange=${exchange}`),
  getTechnicals: (symbol: string, exchange: "NSE" | "BSE" = "NSE") =>
    request<TechnicalsResponse>(`/api/stocks/${symbol}/technicals?exchange=${exchange}`),
  getFundamentals: (symbol: string, exchange: "NSE" | "BSE" = "NSE") =>
    request<FundamentalsResponse>(
      `/api/stocks/${encodeURIComponent(symbol)}/fundamentals?exchange=${exchange}`, {}, 45_000),
  getHistory: (symbol: string, period = "6mo", interval = "1d", exchange: "NSE" | "BSE" = "NSE") =>
    request<{ history: Array<{ date: string; o: number; h: number; l: number; c: number; v: number }> }>(
      `/api/stocks/${symbol}/history?period=${period}&interval=${interval}&exchange=${exchange}`
    ),
  aiAnalysis: (symbol: string, timeframe: "intraday" | "swing" | "long" = "swing") =>
    request<AIAnalysisResponse>(`/api/stocks/${symbol}/ai-analysis`, {
      method: "POST",
      body: JSON.stringify({ timeframe }),
    }, 120_000),  // AI analysis can be slow on cold fundamentals/LLM cache

  // Watchlist
  getWatchlist: () => request<{ watchlist: WatchlistItem[] }>("/api/watchlist"),
  addToWatchlist: (symbol: string, name: string, exchange = "NSE") =>
    request<{ item: WatchlistItem }>("/api/watchlist", {
      method: "POST",
      body: JSON.stringify({ symbol, name, exchange }),
    }),
  removeFromWatchlist: (symbol: string) =>
    request<{ ok: boolean }>(`/api/watchlist/${symbol}`, { method: "DELETE" }),

  // Market
  getIndices: () => request<Record<string, { symbol: string; price: number; change: number; change_pct: number }>>("/api/market/indices"),
  getMarketContext: () => request<{
    fii_dii: { fii_net: number | null; dii_net: number | null; sentiment: string; source: string } | null;
    india_vix: number | null;
    market_regime: { regime: string; confidence: number; description: string } | null;
  }>("/api/market/context", {}, 45_000),

  // Settings
  getSettings: () => request<{ settings: AppSettings }>("/api/settings"),
  updateSettings: (settings: Partial<AppSettings>) =>
    request<{ ok: boolean }>("/api/settings", { method: "POST", body: JSON.stringify(settings) }),
  testUpstox: () =>
    request<{ ok: boolean; message: string; user?: string }>("/api/settings/test-upstox", { method: "POST" }),

  // Alerts
  getAlerts: () =>
    request<{ alerts: Array<{ id: string; symbol: string; target_price: number; condition: string; current_price_at_creation: number | null; created_at: string; triggered_at: string | null; triggered_price: number | null; active: boolean; note: string | null }> }>("/api/alerts"),
  createAlert: (symbol: string, target_price: number, condition: string, note?: string) =>
    request<{ alert: { id: string; symbol: string; target_price: number; condition: string; created_at: string; active: boolean; note: string | null } }>("/api/alerts", {
      method: "POST",
      body: JSON.stringify({ symbol, target_price, condition, ...(note ? { note } : {}) }),
    }),
  deleteAlert: (alertId: string) =>
    request<{ ok: boolean }>(`/api/alerts/${alertId}`, { method: "DELETE" }),

  // Screener
  screenerPreset: (preset: string) =>
    request<{ results: Array<{ symbol: string; name: string; close: number; change_pct: number; rsi: number | null; volume_ratio: number | null; recommendation: string | null }> }>(
      `/api/screener/presets/${encodeURIComponent(preset)}`
    ),

  // Performance
  getPerformanceSummary: (windowDays: number = 30) =>
    request<{
      data: {
        total_evaluated: number;
        total_wins: number;
        win_rate: number;
        avg_pnl_pct: number;
        window_days: number | null;
        last_evaluated_at: string | null;
      };
    }>(`/api/performance/summary?window_days=${windowDays}`),

  // Manual scan is asynchronous: POST returns 202 + job_id quickly, then the
  // client polls `getScanStatus()` until status is "completed" or "failed".
  // Both trigger and status get 30s — the spawn and the read-only status
  // snapshot are O(ms) work, but the event loop is briefly saturated by the
  // concurrent scan workers, so a tight 10s ceiling produces spurious aborts.
  triggerScan: () =>
    request<ScanTriggerResponse>("/api/scan/trigger", { method: "POST" }, 30_000),
  getScanStatus: () =>
    request<ScanStatus>("/api/scan/status", {}, 30_000),

  // ── Tier 1/2/3 added bindings ───────────────────────────────────────
  getNews: (limit = 20) =>
    request<{ news: NewsItem[]; count: number }>(`/api/market/news?limit=${limit}`),
  getCorporateActions: () =>
    request<{ actions: CorporateAction[]; count: number }>("/api/market/actions"),
  getBlockDeals: () =>
    request<{ deals: BlockDeal[]; count: number }>("/api/market/block-deals"),
  getOptionsAnalysis: (symbol: string) =>
    request<OptionsAnalysis>(`/api/market/options/${encodeURIComponent(symbol)}`),

  // Custom screener (parametric)
  customScreener: (params: ScreenerParams) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
    });
    return request<{ count: number; results: Array<{ symbol: string; name: string; close: number; change_pct: number; rsi: number | null; volume_ratio: number | null; recommendation: string | null; sector?: string; market_cap?: number }> }>(
      `/api/screener?${qs.toString()}`
    );
  },
  getScreenerPresets: () =>
    request<{ presets: Record<string, { label: string; description?: string; params: ScreenerParams }> }>("/api/screener/presets"),

  // Backtest
  backtest: (symbol: string, period = "1y", evalDays = 5, exchange: "NSE" | "BSE" = "NSE") =>
    request<BacktestResult>(
      `/api/backtest/${encodeURIComponent(symbol)}?period=${period}&eval_days=${evalDays}&exchange=${exchange}`,
      { method: "POST" }, 90_000),

  // Per-signal-type edge (static, derived from internal backtest)
  getSignalEdge: () => request<SignalEdgeResponse>("/api/performance/edge"),

  // Autonomous-loop endpoints
  getInsights: () => request<InsightsResponse>("/api/performance/insights"),

  // Is the autonomous engine actually running? (heartbeats + schedule)
  getAutomationStatus: () =>
    request<{ data: AutomationStatus }>("/api/performance/automation-status"),

  // Performance breakdown by signal type (live + tracked outcomes).
  getPerformanceByType: (signalType?: string, direction?: string) => {
    const qs = new URLSearchParams();
    if (signalType) qs.set("signal_type", signalType);
    if (direction) qs.set("direction", direction);
    const q = qs.toString();
    return request<{ data: PerformanceByTypeRow[] }>(
      `/api/performance/by-type${q ? `?${q}` : ""}`
    );
  },

  // Last N weekly autonomous backtest runs (newest first).
  getBacktestHistory: (limit = 12) =>
    request<{ runs: BacktestRun[]; count: number }>(
      `/api/performance/backtest-history?limit=${limit}`
    ),

  // ── 9pt.md additions ─────────────────────────────────────────────────

  // #1 Cohort dashboard — WR/avg PnL/Wilson LB since a date floor.
  getCohort: (since?: string) =>
    request<{
      since: string;
      signals: {
        by_type: Array<{
          signal_type: string; direction: string; total: number;
          wins: number; losses: number; expired: number; open: number;
          win_rate: number; wilson_lb: number; avg_pnl_pct: number;
        }>;
        totals: { total: number; wins: number; losses: number; expired: number;
          open: number; win_rate: number; wilson_lb: number };
      };
      recommendations: { total: number; wins: number; losses: number;
        expired: number; open: number; win_rate: number; wilson_lb: number;
        considered_holds: number };
    }>(`/api/performance/cohort${since ? `?since=${encodeURIComponent(since)}` : ""}`),

  // #2 Live market snapshot (FII/DII/VIX/USDINR/Brent + sector rotation).
  getMarketSnapshot: (force = false) =>
    request<{
      data: {
        as_of: string;
        nifty_close: number | null; nifty_pct: number | null;
        bank_nifty_close: number | null; bank_nifty_pct: number | null;
        india_vix: number | null; usd_inr: number | null; brent_usd: number | null;
        fii_net_cr: number | null; dii_net_cr: number | null;
        sector_rotation: string | null;
        sector_movers: Array<{ sector: string; pct5d: number }>;
        stale: boolean;
      };
      briefing: string;
    }>(`/api/market/snapshot${force ? "?force=true" : ""}`),

  // #3 Per-layer LLM cost telemetry.
  getLlmUsage: () =>
    request<{
      today: { tokens: number; costUsd: number; costInr: number };
      mtd: { tokens: number; costUsd: number; costInr: number };
      capUsd: number; capRemainingUsd: number;
      byProvider: Array<{ provider: string; tokens: number; costUsd: number }>;
      byLayerToday: Array<{ route: string; calls: number; tokens: number; costUsd: number }>;
      byLayerMtd: Array<{ route: string; calls: number; tokens: number; costUsd: number }>;
      usdInrRate: number;
    }>("/api/llm/usage"),

  // #4 Conversational chat per signal.
  getSignalChat: (signalId: string, sessionId = "default") =>
    request<{ signal_id: string; session_id: string; messages: Array<{ role: string; content: string }> }>(
      `/api/signals/${encodeURIComponent(signalId)}/chat?session_id=${encodeURIComponent(sessionId)}`
    ),
  postSignalChat: (signalId: string, message: string, sessionId = "default") =>
    request<{ signal_id: string; session_id: string; reply: string }>(
      `/api/signals/${encodeURIComponent(signalId)}/chat`,
      { method: "POST", body: JSON.stringify({ message, session_id: sessionId }) },
      60_000,
    ),
  // Returns the SSE URL — caller wires their own EventSource.
  signalChatStreamUrl: async (signalId: string) =>
    `${await getBackendUrl()}/api/signals/${encodeURIComponent(signalId)}/chat/stream`,
  getSignalReasoning: (signalId: string) =>
    request<{
      signal_id: string;
      judge: { verdict: string | null; reason: string | null };
      debate: { synthesis: string | null };
      multi_perspective: { consensus: string | null; synthesis: string | null };
    }>(`/api/signals/${encodeURIComponent(signalId)}/reasoning`),

  // #7 Options
  getOptionsView: (symbol: string) =>
    request<{
      symbol: string;
      chain: Record<string, unknown>;
      positioning: {
        spot: number | null; max_pain: number | null;
        distance_pct_to_max_pain: number | null;
        anchor_direction: string; pcr_signal: string | null; pcr_oi: number | null;
      };
      unusual_activity: Array<{ strike: number | null; oi: number | null; oi_change: number | null; iv: number | null }>;
    }>(`/api/options/${encodeURIComponent(symbol)}`),
  getIvRankScreener: (gte = 80, universe = "nifty50", limit = 25) =>
    request<{ data: Array<{ symbol: string; atm_iv: number; iv_rank_proxy: number; pcr_oi: number; max_pain: number; spot: number }>; universe: string; gte: number }>(
      `/api/options/screener/iv-rank?gte=${gte}&universe=${encodeURIComponent(universe)}&limit=${limit}`
    ),

  // #8 Portfolio risk dashboard.
  getRiskDashboard: () =>
    request<{
      open_positions: Array<Record<string, unknown>>;
      correlation_matrix: Array<{ symbol: string; max_correlation: number | null; most_correlated_with: string | null }>;
      paper_sector_exposure: Array<{ sector: string; exposure_pct: number }>;
      alerts: Array<{ severity: string; kind: string; message: string }>;
    }>("/api/portfolio/risk-dashboard"),

  // #6 Broker
  getBrokerStatus: () =>
    request<{ broker: string; credentials_present: boolean; last_check_iso: string | null; last_check_ok: boolean }>("/api/broker/status"),
  testBroker: (broker?: string, probeSymbol = "RELIANCE") =>
    request<{ broker: string; ok: boolean; message: string; probe?: unknown }>(
      "/api/broker/test",
      { method: "POST", body: JSON.stringify({ broker, probe_symbol: probeSymbol }) },
      30_000,
    ),
  getKiteLoginUrl: () =>
    request<{ login_url: string; redirect_hint: string | null; note: string }>("/api/broker/kite/login-url"),
  kiteExchangeToken: (requestToken: string) =>
    request<{ ok: boolean; broker: string; message: string }>(
      "/api/broker/kite/exchange-token",
      { method: "POST", body: JSON.stringify({ request_token: requestToken }) },
      30_000,
    ),
  getTradeJournal: (limit = 100, symbol?: string, mode?: "dry_run" | "live") => {
    const qs = new URLSearchParams({ limit: String(limit) });
    if (symbol) qs.set("symbol", symbol);
    if (mode) qs.set("mode", mode);
    return request<{ data: Array<Record<string, unknown>>; count: number }>(
      `/api/broker/journal?${qs.toString()}`
    );
  },
};
