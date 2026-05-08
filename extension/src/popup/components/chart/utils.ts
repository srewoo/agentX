/**
 * Chart math + formatting utilities. Pure, side-effect free.
 *
 * Why local: the brief references `@/lib/format` and `@/lib/types`, but this
 * project doesn't use a path alias. Keeping these helpers next to the only
 * consumers (chart components) avoids reaching into shared/ for code that no
 * other surface needs yet.
 */

export interface Candle {
  time: string | number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export type Interval = "1m" | "5m" | "15m" | "1h" | "1d";
export type Exchange = "NSE" | "BSE";

/** Map a chart interval to a sensible default range/period for `api.getHistory`. */
export function intervalToRange(interval: Interval): string {
  switch (interval) {
    case "1m":
      return "1d";
    case "5m":
      return "5d";
    case "15m":
      return "5d";
    case "1h":
      return "1mo";
    case "1d":
      return "1y";
  }
}

/** Format INR with up to 2 decimals — matches typical NSE quote precision. */
export function formatINRPrecise(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

export function formatChangePct(pct: number | null | undefined): string {
  if (pct == null || !Number.isFinite(pct)) return "—";
  const sign = pct >= 0 ? "+" : "";
  return `${sign}${pct.toFixed(2)}%`;
}

export function formatVolume(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return "—";
  if (v >= 1e7) return `${(v / 1e7).toFixed(2)}Cr`;
  if (v >= 1e5) return `${(v / 1e5).toFixed(2)}L`;
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
  return String(Math.round(v));
}

/** EMA — recursive, SMA-seeded. Synchronous fallback for when worker unavailable. */
export function computeEMA(closes: number[], period: number): (number | null)[] {
  const out: (number | null)[] = new Array(closes.length).fill(null);
  if (closes.length < period) return out;
  const k = 2 / (period + 1);
  let sum = 0;
  for (let i = 0; i < period; i++) sum += closes[i];
  let ema = sum / period;
  out[period - 1] = ema;
  for (let i = period; i < closes.length; i++) {
    ema = closes[i] * k + ema * (1 - k);
    out[i] = ema;
  }
  return out;
}

/** RSI(14), Wilder's smoothing. */
export function computeRSI(closes: number[], period = 14): (number | null)[] {
  const out: (number | null)[] = new Array(closes.length).fill(null);
  if (closes.length < period + 1) return out;
  let gain = 0;
  let loss = 0;
  for (let i = 1; i <= period; i++) {
    const d = closes[i] - closes[i - 1];
    if (d >= 0) gain += d;
    else loss -= d;
  }
  let avgG = gain / period;
  let avgL = loss / period;
  out[period] = avgL === 0 ? 100 : 100 - 100 / (1 + avgG / avgL);
  for (let i = period + 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1];
    const g = d > 0 ? d : 0;
    const l = d < 0 ? -d : 0;
    avgG = (avgG * (period - 1) + g) / period;
    avgL = (avgL * (period - 1) + l) / period;
    out[i] = avgL === 0 ? 100 : 100 - 100 / (1 + avgG / avgL);
  }
  return out;
}

/** MACD(12, 26, 9). Returns macd line, signal line, histogram. */
export function computeMACD(
  closes: number[],
  fast = 12,
  slow = 26,
  signalPeriod = 9
): { macd: (number | null)[]; signal: (number | null)[]; hist: (number | null)[] } {
  const emaFast = computeEMA(closes, fast);
  const emaSlow = computeEMA(closes, slow);
  const macd: (number | null)[] = closes.map((_, i) => {
    const f = emaFast[i];
    const s = emaSlow[i];
    return f != null && s != null ? f - s : null;
  });
  // signal = EMA of macd, but macd has leading nulls — shift, compute, re-align
  const firstIdx = macd.findIndex((v) => v != null);
  const signal: (number | null)[] = new Array(closes.length).fill(null);
  if (firstIdx >= 0) {
    const trimmed = macd.slice(firstIdx).filter((v): v is number => v != null);
    const sigVals = computeEMA(trimmed, signalPeriod);
    for (let i = 0; i < sigVals.length; i++) {
      signal[firstIdx + i] = sigVals[i];
    }
  }
  const hist: (number | null)[] = macd.map((m, i) => {
    const s = signal[i];
    return m != null && s != null ? m - s : null;
  });
  return { macd, signal, hist };
}

/** Convert a parallel (time, value) pair list into chart-ready LineData, dropping nulls. */
export function toLineData<T extends string | number>(
  times: T[],
  values: (number | null)[]
): { time: T; value: number }[] {
  const out: { time: T; value: number }[] = [];
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    if (v != null && Number.isFinite(v)) {
      out.push({ time: times[i], value: v });
    }
  }
  return out;
}

/** Build a screen-reader summary string. */
export function buildA11ySummary(args: {
  symbol: string;
  changePct: number | null;
  lastPrice: number | null;
  rangeLow: number | null;
  rangeHigh: number | null;
  rangeDays?: number;
}): string {
  const { symbol, changePct, lastPrice, rangeLow, rangeHigh, rangeDays = 5 } = args;
  if (lastPrice == null) return `${symbol} chart unavailable.`;
  const dir =
    changePct == null ? "unchanged" : changePct >= 0 ? "up" : "down";
  const pct = changePct == null ? "" : `${Math.abs(changePct).toFixed(2)}%`;
  const range =
    rangeLow != null && rangeHigh != null
      ? `, ${rangeDays}-day range ${formatINRPrecise(rangeLow)}–${formatINRPrecise(rangeHigh)}`
      : "";
  return `${symbol} ${dir} ${pct} today, last price ${formatINRPrecise(
    lastPrice
  )}${range}.`;
}
