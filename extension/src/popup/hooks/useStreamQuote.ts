import { useEffect, useRef, useState } from "react";
import { getBackendUrl, getSettings } from "../../shared/storage";
import type { Quote } from "../lib/types";

// Subprotocol the backend recognises for smuggling the API key through the
// browser-allowed `Sec-WebSocket-Protocol` handshake header.
// Format: `agentx.key.<API_KEY>`. Mirrors `_SUBPROTOCOL_KEY_PREFIX` in
// backend/app/routers/stream.py.
const KEY_SUBPROTOCOL_PREFIX = "agentx.key.";

interface QuoteMap {
  [symbol: string]: Quote;
}

interface SocketBucket {
  socket: WebSocket | null;
  refs: number;
  symbols: Set<string>;
  listeners: Set<(q: Quote) => void>;
  reconnectAttempt: number;
  closing: boolean;
  reconnectTimer: ReturnType<typeof setTimeout> | null;
}

const bucket: SocketBucket = {
  socket: null,
  refs: 0,
  symbols: new Set(),
  listeners: new Set(),
  reconnectAttempt: 0,
  closing: false,
  reconnectTimer: null,
};

function wsUrlFromHttp(
  httpBase: string,
  symbols: string[],
  apiKey: string,
  useSubprotocolForAuth: boolean,
): string {
  const u = new URL(httpBase);
  u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
  u.pathname = u.pathname.replace(/\/$/, "") + "/api/stream/quotes";
  const params = new URLSearchParams();
  params.set("symbols", symbols.join(","));
  // When subprotocol auth isn't being used (or no key is configured), put
  // the key on the query string so the server's auth check still passes.
  if (apiKey && !useSubprotocolForAuth) {
    params.set("api_key", apiKey);
  }
  u.search = `?${params.toString()}`;
  return u.toString();
}

async function ensureSocket() {
  if (bucket.socket && bucket.socket.readyState <= WebSocket.OPEN) return;
  if (bucket.symbols.size === 0) return;
  const base = await getBackendUrl();
  // Pull the configured backend API key (chrome.storage.local under
  // `sensitiveSettings.api_key`). Empty string when auth is not configured.
  const settings = (await getSettings()) as Record<string, string | undefined>;
  const apiKey = settings.api_key ?? "";

  // Prefer subprotocol so the key never appears in URLs (server logs, browser
  // history). Fall back to the query-string transport only when there's no key.
  const useSubprotocol = !!apiKey;
  const url = wsUrlFromHttp(
    base,
    Array.from(bucket.symbols),
    apiKey,
    useSubprotocol,
  );
  bucket.closing = false;
  try {
    const ws = useSubprotocol
      ? new WebSocket(url, [`${KEY_SUBPROTOCOL_PREFIX}${apiKey}`])
      : new WebSocket(url);
    bucket.socket = ws;
    ws.onopen = () => {
      bucket.reconnectAttempt = 0;
    };
    ws.onmessage = (ev) => {
      try {
        const parsed = JSON.parse(ev.data) as Quote | { data: Quote };
        const q = "data" in parsed ? parsed.data : parsed;
        if (!q || typeof q.symbol !== "string") return;
        bucket.listeners.forEach((cb) => cb(q));
      } catch {
        /* ignore malformed frames */
      }
    };
    ws.onerror = () => {
      // onclose will run reconnect
    };
    ws.onclose = () => {
      bucket.socket = null;
      if (bucket.closing || bucket.refs === 0) return;
      // Exponential backoff with jitter, capped at 30s.
      const attempt = ++bucket.reconnectAttempt;
      const delay = Math.min(30_000, 500 * 2 ** Math.min(attempt, 6));
      const jitter = Math.floor(Math.random() * 250);
      bucket.reconnectTimer = setTimeout(() => void ensureSocket(), delay + jitter);
    };
  } catch {
    // network error, retry shortly
    bucket.reconnectTimer = setTimeout(() => void ensureSocket(), 1000);
  }
}

function teardownIfIdle() {
  if (bucket.refs > 0) return;
  bucket.closing = true;
  if (bucket.reconnectTimer) {
    clearTimeout(bucket.reconnectTimer);
    bucket.reconnectTimer = null;
  }
  bucket.socket?.close();
  bucket.socket = null;
  bucket.symbols.clear();
  bucket.listeners.clear();
  bucket.reconnectAttempt = 0;
}

/**
 * Subscribe to live quotes for `symbols`. Returns a map keyed by symbol.
 * Uses a singleton WebSocket per popup instance, ref-counted across hooks.
 */
export function useStreamQuote(symbols: string[]) {
  const [quotes, setQuotes] = useState<QuoteMap>({});
  const symbolsKey = symbols.join(",");
  const listenerRef = useRef<((q: Quote) => void) | null>(null);

  useEffect(() => {
    if (symbols.length === 0) {
      setQuotes({});
      return;
    }

    bucket.refs += 1;
    symbols.forEach((s) => bucket.symbols.add(s));

    const onQuote = (q: Quote) => {
      if (!symbols.includes(q.symbol)) return;
      setQuotes((prev) => ({ ...prev, [q.symbol]: q }));
    };
    listenerRef.current = onQuote;
    bucket.listeners.add(onQuote);

    void ensureSocket();

    return () => {
      bucket.refs = Math.max(0, bucket.refs - 1);
      if (listenerRef.current) bucket.listeners.delete(listenerRef.current);
      // Don't aggressively prune symbols — they'll naturally clear on teardown.
      if (bucket.refs === 0) teardownIfIdle();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [symbolsKey]);

  return quotes;
}
