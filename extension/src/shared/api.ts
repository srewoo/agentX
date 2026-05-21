import { getBackendUrl, getSettings } from "./storage";
import type {
  Signal, StockQuote, TechnicalsResponse, AIAnalysisResponse, WatchlistItem, AppSettings, HealthResponse,
  NewsItem, CorporateAction, OptionsAnalysis, BlockDeal, BacktestResult, ScreenerParams, FundamentalsResponse,
  SignalEdgeResponse, InsightsResponse, BacktestRun, PerformanceByTypeRow, DeepSignalAnalysis,
  ScanTriggerResponse, ScanStatus,
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
  getPerformanceSummary: () =>
    request<{ data: { total_evaluated: number; total_wins: number; win_rate: number; avg_pnl_pct: number } }>("/api/performance/summary"),

  // Manual scan (120s timeout — scan now fetches FII/DII, VIX, delivery volume, RS, etc.)
  // Scan is asynchronous: POST returns 202 + job_id in <1s, then the client
  // polls `getScanStatus()` until status is "completed" or "failed". This
  // avoids HTTP timeouts on real scans which run 160-200s. See watchScan().
  triggerScan: () =>
    request<ScanTriggerResponse>("/api/scan/trigger", { method: "POST" }, 10_000),
  getScanStatus: () =>
    request<ScanStatus>("/api/scan/status", {}, 10_000),

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
};
