/**
 * Frontend ↔ backend contracts for the popup redesign.
 *
 * These shapes are the contract sibling backend agents must match. If the
 * backend changes, update here first, then propagate.
 */

export type Horizon = "intraday" | "swing" | "long";
export type Direction = "BUY" | "SELL" | "HOLD" | "AVOID";
export type Exchange = "NSE" | "BSE";

export interface Recommendation {
  id: string;
  symbol: string;
  name: string;
  exchange: Exchange;
  sector: string | null;
  horizon: Horizon;
  direction: Direction;
  /** 0..1 model conviction. */
  conviction: number;
  /** Triggering reasons / bullets. */
  rationale: string[];
  entryPrice: number | null;
  stopLoss: number | null;
  target: number | null;
  /** ISO timestamp. */
  generatedAt: string;
  /** Canonical backend action, kept when available for richer cards. */
  action?: Direction;
  riskReward?: number;
  target2?: number | null;
  marketCapBand?: "LARGE" | "MID" | "SMALL" | "MICRO";
  lastPrice?: number;
  priceChangePct1d?: number;
  deliveryPct?: number | null;
  fiiDiiSignal?: "INFLOW" | "OUTFLOW" | "NEUTRAL" | null;
  fAndOSignal?: "LONG_BUILDUP" | "SHORT_BUILDUP" | "LONG_UNWINDING" | "SHORT_COVERING" | null;
  timeframeDays?: number;
  regime?: string | null;
  weightedScore?: number | null;
  factorAgreement?: number | null;
  calibrationNote?: string | null;
  dataQuality?: string | null;
  advisoryDisclaimer?: string;
  signals?: ReadonlyArray<{
    name: string;
    weight: number;
    value: number;
    direction: "pos" | "neg" | "neu";
  }>;
}

export interface RecommendationFilters {
  horizon?: Horizon;
  sector?: string;
  /** UI value; accepts either 0..1 or 0..100. */
  minConviction?: number;
}

export interface Holding {
  symbol: string;
  name: string;
  exchange: Exchange;
  qty: number;
  avgPrice: number;
  ltp: number | null;
  marketValue: number | null;
  /** Absolute P&L in INR (alias of `totalPnl` for sibling components). */
  pnl: number | null;
  /** Fractional, e.g. 0.0234 = +2.34%. */
  pnlPct: number | null;
  dayChangePct: number | null;
  /** Sibling-component aliases — keep both shapes valid so consumers can
   *  pick the name that reads best in context. */
  dayPnl?: number | null;
  totalPnl?: number | null;
  sector?: string | null;
}

export interface PortfolioSummary {
  invested: number;
  marketValue: number;
  pnl: number;
  pnlPct: number;
  dayPnl: number;
  dayPnlPct: number;
  holdings: Holding[];
  /** ISO timestamps + value points for equity curve. */
  equityCurve: EquityPoint[];
  // Optional analytics fields used by `PortfolioSummary` card. All are
  // best-effort: backend may omit any of them; UI must handle null.
  totalPnl?: number;
  totalPnlPct?: number;
  totalValue?: number;
  capital?: number;
  sharpe?: number | null;
  maxDrawdown?: number | null;
  beta?: number | null;
  /** Closed-trade win rate, fraction 0..1. */
  winRate?: number | null;
  /** Gross-profit / gross-loss ratio. */
  profitFactor?: number | null;
}

export interface Alert {
  id: string;
  symbol: string;
  condition: "above" | "below" | "crosses_above" | "crosses_below";
  targetPrice: number;
  note: string | null;
  active: boolean;
  createdAt: string;
  triggeredAt: string | null;
  triggeredPrice: number | null;
}

export interface LlmUsage {
  /** Tokens used today. */
  used: number;
  /** Configured daily cap. */
  cap: number;
  /** Cost in INR for the day. */
  costInr: number;
  /** ISO date for the period (YYYY-MM-DD). */
  day: string;
}

export interface Quote {
  symbol: string;
  ltp: number;
  change: number;
  changePct: number;
  /** ISO. */
  ts: string;
}

export interface ApiEnvelope<T> {
  data: T;
  meta?: Record<string, unknown>;
  errors?: Array<{ code: string; message: string; field?: string }>;
}

export type MarketStatus = "PRE_OPEN" | "OPEN" | "CLOSED" | "POST_CLOSE";

/* ── Sibling-component shared types ─────────────────────────────────────
 *  Imported via `@/lib/types` by the recommendation-ui, charts, and
 *  portfolio agents. Keep these stable — extending is fine, breaking is not.
 */

/** Equity-curve point — alias retained for sibling-agent imports.
 *  Both naming conventions are populated server-side so consumers can use
 *  whichever reads better in context. */
export interface EquityPoint {
  t: string;
  v: number;
  date: string;
  value: number;
}

/** Sector exposure for the Portfolio "SectorAllocation" donut. */
export interface SectorExposure {
  sector: string;
  marketValue: number;
  weight: number; // 0..1
  pnl: number;
  pnlPct: number; // 0..1 fraction
  /** Day's contribution % from this sector. */
  dayChange?: number | null;
}

// Lowercase to match the paper-trading and add-transaction dialogs that
// expect "buy" / "sell". UI components should normalise display casing.
export type TransactionSide = "buy" | "sell" | "BUY" | "SELL";

export interface Transaction {
  id: string;
  symbol: string;
  side: TransactionSide;
  qty: number;
  price: number;
  fees: number;
  /** ISO timestamp (canonical). */
  executedAt: string;
  /** Alias used by some sibling lists. */
  timestamp?: string;
  exchange?: Exchange;
  note: string | null;
}

export interface NewTransactionInput {
  symbol: string;
  side: TransactionSide;
  qty: number;
  price: number;
  fees?: number;
  executedAt?: string;
  note?: string;
  exchange?: Exchange;
}

export interface TransactionsPage {
  items: Transaction[];
  /** Cursor for next page, null when end. */
  nextCursor: string | null;
  /** Sibling alias — some consumers paginate `{ data, meta }`. The api
   *  facade always populates both shapes so consumers don't need guards. */
  data: Transaction[];
  meta: { nextCursor: string | null; total?: number };
}

/** Friendlier alias for sibling code. */
export type PortfolioSummaryData = PortfolioSummary;
