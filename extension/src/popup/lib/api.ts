/**
 * Typed API client for the agentX popup.
 *
 * Contracts (must match backend response shapes):
 *  - GET  /api/v1/recommendations?horizon&sector&minConviction → Recommendation[]
 *  - GET  /api/v1/portfolio                                    → PortfolioSummary
 *  - GET  /api/v1/portfolio/holdings                           → Holding[]
 *  - GET  /api/v1/alerts                                       → Alert[]
 *  - POST /api/v1/alerts                                       → Alert
 *  - DEL  /api/v1/alerts/:id                                   → { ok: true }
 *  - GET  /api/v1/usage/llm                                    → LlmUsage
 *  - WS   /api/stream/quotes?symbols=A,B                       → Quote (per msg)
 *
 * All JSON responses are wrapped in `{ data, meta?, errors? }` envelope.
 * The client unwraps `data` and surfaces `errors[0]` as a thrown ApiError.
 */

import { getBackendUrl, getSettings } from "../../shared/storage";
import type {
  Alert,
  ApiEnvelope,
  Holding,
  LlmUsage,
  NewTransactionInput,
  PortfolioSummary,
  Recommendation,
  RecommendationFilters,
  Transaction,
  TransactionsPage,
} from "./types";

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(message: string, status: number, code = "UNKNOWN") {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

const DEFAULT_TIMEOUT = 30_000;

/** Tiny RFC4122 v4 generator using crypto when available. */
export function uuidv4(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  // Fallback — not crypto-strong but unique enough for request IDs.
  const rnd = () => Math.floor(Math.random() * 0x10000).toString(16).padStart(4, "0");
  return `${rnd()}${rnd()}-${rnd()}-4${rnd().slice(1)}-${rnd()}-${rnd()}${rnd()}${rnd()}`;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  timeoutMs?: number;
  query?: Record<string, string | number | boolean | undefined | null>;
}

function buildQuery(params?: RequestOptions["query"]): string {
  if (!params) return "";
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    usp.set(k, String(v));
  }
  const s = usp.toString();
  return s ? `?${s}` : "";
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const baseUrl = await getBackendUrl();
  const settings = (await getSettings()) as Record<string, string>;
  const apiKey = settings.api_key || "";
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT;

  const requestId = uuidv4();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-Request-Id": requestId,
    ...(apiKey ? { "X-API-Key": apiKey } : {}),
  };

  // Compose abort: caller signal + internal timeout.
  const controller = new AbortController();
  const onAbort = () => controller.abort();
  if (opts.signal) {
    if (opts.signal.aborted) controller.abort();
    else opts.signal.addEventListener("abort", onAbort, { once: true });
  }
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  const url = `${baseUrl}${path}${buildQuery(opts.query)}`;

  try {
    const res = await fetch(url, {
      method: opts.method ?? "GET",
      headers,
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: controller.signal,
    });

    let payload: unknown = null;
    const text = await res.text();
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        // non-JSON response
        payload = { errors: [{ code: "BAD_JSON", message: text.slice(0, 200) }] };
      }
    }

    if (!res.ok) {
      const env = (payload as ApiEnvelope<unknown> | null) ?? null;
      const first = env?.errors?.[0];
      throw new ApiError(
        first?.message || `HTTP ${res.status}`,
        res.status,
        first?.code || `HTTP_${res.status}`,
      );
    }

    const env = payload as ApiEnvelope<T>;
    if (env && env.errors && env.errors.length > 0) {
      const first = env.errors[0];
      throw new ApiError(first.message, res.status, first.code);
    }
    // Tolerate either envelope or bare data — sibling backends may not have envelope yet.
    if (env && "data" in (env as object)) return env.data as T;
    return payload as T;
  } catch (err) {
    if (err instanceof ApiError) throw err;
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new ApiError(`Request timed out after ${timeoutMs}ms: ${path}`, 0, "TIMEOUT");
    }
    if (err instanceof Error) {
      throw new ApiError(err.message, 0, "NETWORK");
    }
    throw new ApiError("Unknown error", 0, "UNKNOWN");
  } finally {
    clearTimeout(timer);
    if (opts.signal) opts.signal.removeEventListener("abort", onAbort);
  }
}

// Backend response shapes (snake_case, sometimes wrapped). We normalise to the
// frontend types in this file so views never crash on a missing field.
type RawHoldingsResp = { holdings?: unknown[] } | unknown[] | null;
type RawSummaryResp = Partial<{
  market_value: number; invested: number; total_pnl: number; pnl: number;
  day_pnl: number; day_pnl_pct: number; pnl_pct: number;
  equity_curve: Array<{ t: string; v: number }>;
}> | null;
type RawLlmUsageResp = Partial<{
  today: { tokens?: number; costUsd?: number; costInr?: number };
  capUsd: number;
}> | null;

const asArray = <T>(v: unknown): T[] => (Array.isArray(v) ? (v as T[]) : []);

type RawFactor = {
  name?: string;
  weight?: number;
  value?: number | null;
  score?: number;
  direction?: "bullish" | "bearish" | "neutral" | "pos" | "neg" | "neu";
};

type RawRecommendation = Partial<{
  id: string;
  symbol: string;
  name: string;
  exchange: "NSE" | "BSE";
  horizon: "intraday" | "swing" | "positional" | "long";
  action: "BUY" | "SELL" | "HOLD" | "AVOID";
  direction: "BUY" | "SELL" | "HOLD" | "AVOID";
  conviction: number;
  entry: number;
  entryPrice: number;
  stoploss: number;
  stopLoss: number;
  target1: number;
  target: number;
  target2: number | null;
  risk_reward: number;
  riskReward: number;
  timeframe_days: number;
  timeframeDays: number;
  reasons: string[];
  rationale: string[];
  sector: string | null;
  market_cap_band: Recommendation["marketCapBand"];
  marketCapBand: Recommendation["marketCapBand"];
  last_price: number;
  lastPrice: number;
  price_change_pct_1d: number;
  priceChangePct1d: number;
  delivery_pct: number | null;
  deliveryPct: number | null;
  fii_dii_signal: Recommendation["fiiDiiSignal"];
  fiiDiiSignal: Recommendation["fiiDiiSignal"];
  f_and_o_signal: Recommendation["fAndOSignal"];
  fAndOSignal: Recommendation["fAndOSignal"];
  generated_at: string;
  generatedAt: string;
  regime: string | null;
  weighted_score: number | null;
  weightedScore: number | null;
  factor_agreement: number | null;
  factorAgreement: number | null;
  calibration_note: string | null;
  calibrationNote: string | null;
  data_quality: string | null;
  dataQuality: string | null;
  advisory_disclaimer: string;
  advisoryDisclaimer: string;
  signals: RawFactor[];
}>;

function looksLikeRecommendation(v: unknown): v is RawRecommendation {
  if (!v || typeof v !== "object") return false;
  const r = v as RawRecommendation;
  return typeof r.symbol === "string" && (
    typeof r.action === "string" ||
    typeof r.direction === "string" ||
    typeof r.generated_at === "string" ||
    typeof r.entry === "number" ||
    typeof r.entryPrice === "number"
  );
}

function normalizeConviction(v: number | undefined): number {
  if (typeof v !== "number" || Number.isNaN(v)) return 0;
  return v > 1 ? v / 100 : v;
}

function normalizeFactorDirection(d: RawFactor["direction"]): "pos" | "neg" | "neu" {
  if (d === "bullish" || d === "pos") return "pos";
  if (d === "bearish" || d === "neg") return "neg";
  return "neu";
}

function normalizeRecommendation(raw: unknown): Recommendation {
  if (!looksLikeRecommendation(raw)) return raw as Recommendation;

  const action = raw.action ?? raw.direction ?? "HOLD";
  const horizon = raw.horizon === "positional" ? "long" : (raw.horizon ?? "swing");
  const generatedAt = raw.generatedAt ?? raw.generated_at ?? new Date().toISOString();
  const symbol = (raw.symbol ?? "").toUpperCase();

  return {
    id: raw.id ?? `${symbol}:${horizon}:${generatedAt}`,
    symbol,
    name: raw.name ?? symbol,
    exchange: raw.exchange ?? "NSE",
    sector: raw.sector ?? null,
    horizon,
    direction: action,
    action,
    conviction: normalizeConviction(raw.conviction),
    rationale: raw.rationale ?? raw.reasons ?? [],
    entryPrice: raw.entryPrice ?? raw.entry ?? null,
    stopLoss: raw.stopLoss ?? raw.stoploss ?? null,
    target: raw.target ?? raw.target1 ?? null,
    target2: raw.target2 ?? null,
    riskReward: raw.riskReward ?? raw.risk_reward,
    generatedAt,
    marketCapBand: raw.marketCapBand ?? raw.market_cap_band,
    lastPrice: raw.lastPrice ?? raw.last_price,
    priceChangePct1d: raw.priceChangePct1d ?? raw.price_change_pct_1d,
    deliveryPct: raw.deliveryPct ?? raw.delivery_pct ?? null,
    fiiDiiSignal: raw.fiiDiiSignal ?? raw.fii_dii_signal ?? null,
    fAndOSignal: raw.fAndOSignal ?? raw.f_and_o_signal ?? null,
    timeframeDays: raw.timeframeDays ?? raw.timeframe_days,
    regime: raw.regime ?? null,
    weightedScore: raw.weightedScore ?? raw.weighted_score ?? null,
    factorAgreement: raw.factorAgreement ?? raw.factor_agreement ?? null,
    calibrationNote: raw.calibrationNote ?? raw.calibration_note ?? null,
    dataQuality: raw.dataQuality ?? raw.data_quality ?? null,
    advisoryDisclaimer:
      raw.advisoryDisclaimer ??
      raw.advisory_disclaimer ??
      "Research signal only, not investment advice. Validate independently and use your own risk controls.",
    signals: Array.isArray(raw.signals)
      ? raw.signals.map((s) => ({
          name: s.name ?? "factor",
          weight: typeof s.weight === "number" ? s.weight : 0,
          value: typeof s.score === "number"
            ? Math.abs(s.score)
            : typeof s.value === "number"
              ? Math.abs(s.value)
              : 0,
          direction: normalizeFactorDirection(s.direction),
        }))
      : [],
  };
}

export const apiClient = {
  async getRecommendations(filters: RecommendationFilters = {}, signal?: AbortSignal): Promise<Recommendation[]> {
    const r = await request<unknown>("/api/recommendations", {
      query: {
        horizon: filters.horizon,
        sector: filters.sector,
        min_conviction:
          typeof filters.minConviction === "number"
            ? Math.round(filters.minConviction <= 1 ? filters.minConviction * 100 : filters.minConviction)
            : undefined,
      },
      signal,
    });
    return asArray<unknown>(r).map(normalizeRecommendation);
  },

  async getPortfolio(signal?: AbortSignal): Promise<PortfolioSummary> {
    const r = (await request<RawSummaryResp>("/api/portfolio/summary", { signal })) ?? {};
    const invested = r.invested ?? 0;
    const marketValue = r.market_value ?? invested;
    const pnl = r.total_pnl ?? r.pnl ?? 0;
    const dayPnl = r.day_pnl ?? 0;
    const pnlPct = r.pnl_pct ?? (invested > 0 ? pnl / invested : 0);
    // Both `{t,v}` (canonical) and `{date,value}` (sibling alias) populated.
    const rawCurve = asArray<{ t?: string; v?: number; date?: string; value?: number }>(r.equity_curve);
    const equityCurve = rawCurve.map((p) => {
      const t = p.t ?? p.date ?? "";
      const v = p.v ?? p.value ?? 0;
      return { t, v, date: t, value: v };
    });
    return {
      invested,
      marketValue,
      pnl,
      pnlPct,
      dayPnl,
      dayPnlPct: r.day_pnl_pct ?? (marketValue > 0 ? dayPnl / marketValue : 0),
      holdings: [],
      equityCurve,
      // Convenience aliases for the PortfolioSummary card.
      totalPnl: pnl,
      totalPnlPct: pnlPct,
      totalValue: marketValue,
      capital: invested,
    };
  },

  async getHoldings(signal?: AbortSignal): Promise<Holding[]> {
    const r = await request<RawHoldingsResp>("/api/portfolio/holdings", { signal });
    if (Array.isArray(r)) return r as Holding[];
    return asArray<Holding>(r?.holdings);
  },

  async getAlerts(signal?: AbortSignal): Promise<Alert[]> {
    const r = await request<unknown>("/api/alerts", { signal });
    return asArray<Alert>(r);
  },

  createAlert(input: { symbol: string; condition: Alert["condition"]; targetPrice: number; note?: string }): Promise<Alert> {
    return request<Alert>("/api/alerts", { method: "POST", body: input });
  },

  deleteAlert(id: string): Promise<{ ok: true }> {
    return request<{ ok: true }>(`/api/alerts/${encodeURIComponent(id)}`, { method: "DELETE" });
  },

  // Portfolio namespace — a thin facade so sibling components can call
  // `client.portfolio.summary()` / `holdings()` / `transactions()` /
  // `addTransaction()` without each component re-implementing the wire
  // shape. Backend will gain matching endpoints; today's API stubs return
  // safe defaults so the UI compiles and renders empty states cleanly.
  portfolio: {
    summary: (signal?: AbortSignal): Promise<PortfolioSummary> =>
      apiClient.getPortfolio(signal),
    holdings: (signal?: AbortSignal): Promise<Holding[]> =>
      apiClient.getHoldings(signal),
    async transactions(_cursor?: string | null, signal?: AbortSignal): Promise<TransactionsPage> {
      try {
        const r = await request<unknown>("/api/portfolio/transactions", { signal });
        if (Array.isArray(r)) {
          return { items: r as Transaction[], data: r as Transaction[], nextCursor: null, meta: { nextCursor: null } };
        }
        const env = (r as { items?: Transaction[]; data?: Transaction[]; nextCursor?: string | null }) ?? {};
        const items = env.items ?? env.data ?? [];
        return { items, data: items, nextCursor: env.nextCursor ?? null, meta: { nextCursor: env.nextCursor ?? null } };
      } catch {
        return { items: [], data: [], nextCursor: null, meta: { nextCursor: null } };
      }
    },
    async addTransaction(input: NewTransactionInput): Promise<Transaction> {
      // Normalise side casing for the backend (which expects upper-case).
      const body = { ...input, side: String(input.side).toUpperCase() };
      return request<Transaction>("/api/portfolio/transactions", { method: "POST", body });
    },
    // Alias for sibling components that use `createTransaction(...)`.
    createTransaction: (input: NewTransactionInput): Promise<Transaction> =>
      apiClient.portfolio.addTransaction(input),
    // Equity curve as a standalone endpoint — falls back to deriving from
    // `summary()` when the dedicated endpoint isn't deployed yet.
    async equityCurve(signal?: AbortSignal): Promise<import("./types").EquityPoint[]> {
      try {
        const r = await request<unknown>("/api/portfolio/equity-curve", { signal });
        const arr = Array.isArray(r) ? r : [];
        return arr.map((p) => {
          const o = (p ?? {}) as { t?: string; v?: number; date?: string; value?: number };
          const t = o.t ?? o.date ?? "";
          const v = o.v ?? o.value ?? 0;
          return { t, v, date: t, value: v };
        });
      } catch {
        const s = await apiClient.getPortfolio(signal);
        return s.equityCurve;
      }
    },
    async sectorExposure(signal?: AbortSignal): Promise<import("./types").SectorExposure[]> {
      try {
        const r = await request<unknown>("/api/portfolio/sectors", { signal });
        const arr = Array.isArray(r) ? r : [];
        return arr as import("./types").SectorExposure[];
      } catch {
        return [];
      }
    },
  },

  async getLlmUsage(signal?: AbortSignal): Promise<LlmUsage> {
    const r = (await request<RawLlmUsageResp>("/api/llm/usage", { signal })) ?? {};
    const today = r.today ?? {};
    const tokens = today.tokens ?? 0;
    // Backend cap is in USD; convert to "tokens" for the existing UI by treating
    // 1 USD ≈ 100k tokens as a coarse visual proxy until the backend ships a
    // token cap directly. The spend bar still drives off cost; this just keeps
    // the legacy UI string non-NaN.
    const capTokens = Math.round(((r.capUsd ?? 0) || 0) * 100_000) || tokens || 1;
    return {
      used: tokens,
      cap: capTokens,
      costInr: today.costInr ?? 0,
      day: new Date().toISOString().slice(0, 10),
    };
  },
};

export type { Recommendation, PortfolioSummary, Holding, Alert, LlmUsage } from "./types";

/** Friendlier alias used by sibling components. */
export const api = apiClient;

// Re-export the live-quote hook so `import { useStreamQuote } from "@/lib/api"` works.
export { useStreamQuote } from "../hooks/useStreamQuote";
