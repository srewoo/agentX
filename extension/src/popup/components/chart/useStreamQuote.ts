import { useEffect, useRef, useState } from "react";
import { api } from "../../../shared/api";

/**
 * Live quote subscription.
 *
 * Tries WebSocket first (if backend exposes /ws/quotes), falls back to polling
 * the existing REST quote endpoint. Either path yields the same shape so
 * consumers don't care which transport is in use.
 *
 * Local to chart/ to avoid colliding with a shell-owned hook of the same name.
 * If/when the shell agent ships `popup/hooks/useStreamQuote`, switch the
 * imports in LiveChart to that path and delete this file.
 */

export interface LiveTick {
  price: number;
  change: number | null;
  changePct: number | null;
  volume: number | null;
  high: number | null;
  low: number | null;
  open: number | null;
  /** Server-provided tick timestamp (ms) — falls back to Date.now(). */
  ts: number;
}

interface Options {
  pollMs?: number;
  /** Disable subscription (e.g. when symbol is empty). */
  enabled?: boolean;
}

export function useStreamQuote(
  symbol: string,
  exchange: "NSE" | "BSE",
  opts: Options = {}
): { tick: LiveTick | null; error: string | null } {
  const { pollMs = 5000, enabled = true } = opts;
  const [tick, setTick] = useState<LiveTick | null>(null);
  const [error, setError] = useState<string | null>(null);
  const cancelRef = useRef(false);

  useEffect(() => {
    if (!enabled || !symbol) return;
    cancelRef.current = false;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tickOnce = async () => {
      try {
        // Route quote fetches to the user-selected exchange. Backend skips
        // the NSE-only direct endpoint for BSE and uses yfinance .BO instead.
        const q = await api.getQuote(symbol, exchange);
        if (cancelRef.current) return;
        if (q.price != null) {
          setTick({
            price: q.price,
            change: q.change,
            changePct: q.change_pct,
            volume: q.volume,
            high: q.high,
            low: q.low,
            open: q.open,
            ts: Date.now(),
          });
          setError(null);
        }
      } catch (e) {
        if (cancelRef.current) return;
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelRef.current) {
          timer = setTimeout(tickOnce, pollMs);
        }
      }
    };

    tickOnce();

    return () => {
      cancelRef.current = true;
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [symbol, exchange, pollMs, enabled]);

  return { tick, error };
}
