/// <reference lib="webworker" />

/**
 * EMA worker — keeps overlay computation off the popup main thread.
 * Triggered for series with > 5k bars.
 *
 * Message in:  { id, closes: number[], periods: number[] }
 * Message out: { id, emas: Record<number, (number|null)[]> }
 */

interface InMsg {
  id: number;
  closes: number[];
  periods: number[];
}

interface OutMsg {
  id: number;
  emas: Record<number, (number | null)[]>;
}

function ema(closes: number[], period: number): (number | null)[] {
  const out: (number | null)[] = new Array(closes.length).fill(null);
  if (closes.length < period) return out;
  const k = 2 / (period + 1);
  let sum = 0;
  for (let i = 0; i < period; i++) sum += closes[i];
  let val = sum / period;
  out[period - 1] = val;
  for (let i = period; i < closes.length; i++) {
    val = closes[i] * k + val * (1 - k);
    out[i] = val;
  }
  return out;
}

self.onmessage = (e: MessageEvent<InMsg>) => {
  const { id, closes, periods } = e.data;
  const emas: Record<number, (number | null)[]> = {};
  for (const p of periods) {
    emas[p] = ema(closes, p);
  }
  const out: OutMsg = { id, emas };
  (self as unknown as Worker).postMessage(out);
};

export {};
