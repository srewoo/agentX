/**
 * Single-flight, in-memory cache of the /api/performance/edge response.
 * Multiple SignalCards mount in parallel — without this, each card would
 * fetch the same static edge table independently. We share one promise so
 * a Dashboard with N cards causes 1 backend hit, not N.
 *
 * The edge table is static (regenerated when the backtest is rerun) so a
 * 30-min in-memory TTL is more than enough.
 */
import { api } from "./api";
import type { SignalEdgeResponse, SignalEdgeRow } from "./types";

const TTL_MS = 30 * 60 * 1000;

let inflight: Promise<SignalEdgeResponse> | null = null;
let cache: { value: SignalEdgeResponse; ts: number } | null = null;

export async function loadEdge(): Promise<SignalEdgeResponse> {
  const now = Date.now();
  if (cache && now - cache.ts < TTL_MS) return cache.value;
  if (inflight) return inflight;
  inflight = api.getSignalEdge()
    .then((res) => {
      cache = { value: res, ts: now };
      return res;
    })
    .finally(() => { inflight = null; });
  return inflight;
}

export async function getEdgeFor(signalType: string, direction: string): Promise<SignalEdgeRow | null> {
  try {
    const res = await loadEdge();
    return res.rows.find((r) => r.signal_type === signalType && r.direction === direction) ?? null;
  } catch {
    return null;
  }
}
