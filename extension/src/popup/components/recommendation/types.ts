/**
 * Local view types for the recommendation UI.
 *
 * The canonical `Recommendation` from `@/lib/types` is intentionally narrow
 * (id, symbol, sector, horizon, direction, conviction 0..1, rationale[],
 * entryPrice, stopLoss, target, generatedAt). The richer Indian-market
 * surface area used by these components (R:R, factor signals, FII/DII flows,
 * F&O positioning, market-cap band, day change, etc.) is layered on top here
 * as optional fields. Backends that don't supply these will simply hide the
 * related chip — no broken UI.
 */

import type { Recommendation as BaseRecommendation } from "@/lib/types";

export type FiiDiiSignal = "INFLOW" | "OUTFLOW" | "NEUTRAL";
export type FAndOSignal =
  | "LONG_BUILDUP"
  | "SHORT_BUILDUP"
  | "LONG_UNWINDING"
  | "SHORT_COVERING";
export type MarketCapBand = "LARGE" | "MID" | "SMALL" | "MICRO";

export type FactorDirection = "pos" | "neg" | "neu";

export interface FactorSignal {
  name: string;
  /** 0..1 weight for this factor in the model. */
  weight: number;
  /** 0..1 normalized factor value. */
  value: number;
  direction: FactorDirection;
}

/**
 * Display-shape recommendation. All extended fields optional so we can render
 * `BaseRecommendation` directly (after a small adapter) and surface richer
 * data when the backend grows up.
 */
export interface RecommendationView extends BaseRecommendation {
  // Extended optional surface
  riskReward?: number;
  target2?: number | null;
  marketCapBand?: MarketCapBand;
  lastPrice?: number;
  priceChangePct1d?: number;
  deliveryPct?: number | null;
  fiiDiiSignal?: FiiDiiSignal | null;
  fAndOSignal?: FAndOSignal | null;
  signals?: readonly FactorSignal[];
  timeframeDays?: number;
  regime?: string | null;
  weightedScore?: number | null;
  factorAgreement?: number | null;
  calibrationNote?: string | null;
  dataQuality?: string | null;
  portfolioContext?: BaseRecommendation["portfolioContext"];
  fundamentalValuation?: BaseRecommendation["fundamentalValuation"];
  ensemble?: BaseRecommendation["ensemble"];
  llmJudge?: BaseRecommendation["llmJudge"];
  advisoryDisclaimer?: string;
}

/**
 * Compute a sensible R:R from entry/stop/target when the backend doesn't ship one.
 * Returns 0 when inputs are insufficient.
 */
export function deriveRiskReward(rec: RecommendationView): number {
  if (typeof rec.riskReward === "number" && rec.riskReward > 0) return rec.riskReward;
  const { entryPrice, stopLoss, target } = rec;
  if (entryPrice == null || stopLoss == null || target == null) return 0;
  const risk = Math.abs(entryPrice - stopLoss);
  if (risk === 0) return 0;
  const reward = Math.abs(target - entryPrice);
  return reward / risk;
}

/**
 * Map the narrow Direction (BUY/SELL/HOLD) to the richer Action vocabulary
 * (BUY/SELL/HOLD/AVOID) used inside cards.
 */
export type Action = "BUY" | "SELL" | "HOLD" | "AVOID";

export function actionFor(rec: RecommendationView): Action {
  // If backend provides an explicit `action`, honour it.
  const maybe = (rec as { action?: Action }).action;
  if (maybe === "BUY" || maybe === "SELL" || maybe === "HOLD" || maybe === "AVOID") return maybe;
  return rec.direction;
}

/** Normalise conviction. Backend type is 0..1 — we display in 0..100. */
export function convictionPct(rec: RecommendationView): number {
  const c = rec.conviction;
  if (Number.isNaN(c)) return 0;
  // Tolerate either fraction (0..1) or already-percent (0..100).
  if (c <= 1) return Math.round(c * 100);
  return Math.round(c);
}
