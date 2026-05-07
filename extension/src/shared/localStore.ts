/**
 * Client-side persistent stores backed by chrome.storage.local.
 * Used for data that doesn't belong on the backend: paper trades,
 * holdings, watchlist groups, mutes, custom screener presets,
 * deep-link handoff target.
 */
import type { PaperTrade, Holding, WatchlistGroup, CustomScreenerPreset } from "./types";

const KEYS = {
  paperTrades: "paperTrades",
  holdings: "holdings",
  watchlistGroups: "watchlistGroups",
  customPresets: "customScreenerPresets",
  deepLinkTarget: "deepLinkTarget", // { symbol, ts } — read once by popup on open
  pinnedSignal: "pinnedSignal", // signal id from notification click
} as const;

async function get<T>(key: string, def: T): Promise<T> {
  const r = await chrome.storage.local.get(key);
  return (r[key] as T) ?? def;
}
async function set<T>(key: string, value: T): Promise<void> {
  await chrome.storage.local.set({ [key]: value });
}

// Paper trades ────────────────────────────────────────────────────────
export const paperTrades = {
  list: () => get<PaperTrade[]>(KEYS.paperTrades, []),
  save: (trades: PaperTrade[]) => set(KEYS.paperTrades, trades),
  add: async (t: PaperTrade) => {
    const all = await paperTrades.list();
    await paperTrades.save([t, ...all]);
  },
  update: async (id: string, patch: Partial<PaperTrade>) => {
    const all = await paperTrades.list();
    await paperTrades.save(all.map((t) => (t.id === id ? { ...t, ...patch } : t)));
  },
  remove: async (id: string) => {
    const all = await paperTrades.list();
    await paperTrades.save(all.filter((t) => t.id !== id));
  },
};

// Holdings ────────────────────────────────────────────────────────────
export const holdings = {
  list: () => get<Holding[]>(KEYS.holdings, []),
  save: (h: Holding[]) => set(KEYS.holdings, h),
  upsert: async (h: Holding) => {
    const all = await holdings.list();
    const idx = all.findIndex((x) => x.symbol === h.symbol);
    if (idx >= 0) all[idx] = h;
    else all.push(h);
    await holdings.save(all);
  },
  remove: async (symbol: string) => {
    const all = await holdings.list();
    await holdings.save(all.filter((x) => x.symbol !== symbol));
  },
};

// Watchlist groups (overlay over backend watchlist) ───────────────────
export const watchlistGroups = {
  list: () => get<WatchlistGroup[]>(KEYS.watchlistGroups, []),
  save: (g: WatchlistGroup[]) => set(KEYS.watchlistGroups, g),
  setGroup: async (symbol: string, group: string) => {
    const all = await watchlistGroups.list();
    const idx = all.findIndex((x) => x.symbol === symbol);
    if (idx >= 0) all[idx] = { symbol, group };
    else all.push({ symbol, group });
    await watchlistGroups.save(all);
  },
};

// Custom screener presets ─────────────────────────────────────────────
export const customPresets = {
  list: () => get<CustomScreenerPreset[]>(KEYS.customPresets, []),
  save: (p: CustomScreenerPreset[]) => set(KEYS.customPresets, p),
  add: async (p: CustomScreenerPreset) => {
    const all = await customPresets.list();
    await customPresets.save([...all, p]);
  },
  remove: async (id: string) => {
    const all = await customPresets.list();
    await customPresets.save(all.filter((x) => x.id !== id));
  },
};

// Deep-link handoff (right-click → analyze, notification → signal) ────
export const deepLink = {
  setSymbol: (symbol: string) =>
    set(KEYS.deepLinkTarget, { symbol, ts: Date.now() }),
  /** Reads and clears the handoff. Returns null if absent or older than 60s. */
  consume: async (): Promise<string | null> => {
    const r = await get<{ symbol: string; ts: number } | null>(KEYS.deepLinkTarget, null);
    if (!r) return null;
    await chrome.storage.local.remove(KEYS.deepLinkTarget);
    if (Date.now() - r.ts > 60_000) return null;
    return r.symbol;
  },
  setPinnedSignal: (id: string) => set(KEYS.pinnedSignal, { id, ts: Date.now() }),
  consumePinnedSignal: async (): Promise<string | null> => {
    const r = await get<{ id: string; ts: number } | null>(KEYS.pinnedSignal, null);
    if (!r) return null;
    await chrome.storage.local.remove(KEYS.pinnedSignal);
    if (Date.now() - r.ts > 60_000) return null;
    return r.id;
  },
};

// CSV helpers ─────────────────────────────────────────────────────────
export function toCSV(rows: Array<Record<string, unknown>>): string {
  if (!rows.length) return "";
  const headers = Object.keys(rows[0]);
  const escape = (v: unknown) => {
    const s = v == null ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [
    headers.join(","),
    ...rows.map((r) => headers.map((h) => escape(r[h])).join(",")),
  ].join("\n");
}

export function parseCSV(text: string): Array<Record<string, string>> {
  const lines = text.split(/\r?\n/).filter((l) => l.trim().length > 0);
  if (lines.length < 2) return [];
  const splitLine = (line: string): string[] => {
    const out: string[] = [];
    let cur = "";
    let inQ = false;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (inQ) {
        if (c === '"' && line[i + 1] === '"') { cur += '"'; i++; }
        else if (c === '"') inQ = false;
        else cur += c;
      } else {
        if (c === ",") { out.push(cur); cur = ""; }
        else if (c === '"') inQ = true;
        else cur += c;
      }
    }
    out.push(cur);
    return out;
  };
  const headers = splitLine(lines[0]).map((h) => h.trim());
  return lines.slice(1).map((line) => {
    const cells = splitLine(line);
    const row: Record<string, string> = {};
    headers.forEach((h, i) => { row[h] = (cells[i] ?? "").trim(); });
    return row;
  });
}

/**
 * Map an iterable through `fn` with a concurrency cap. Use for fan-out fetches
 * (watchlist quotes, holdings, paper trades) so we don't fire 50 parallel
 * requests at the backend, which then cascades to NSE / yfinance rate limits.
 * Default cap is 4 — friendly to backend cache warmup without serializing.
 */
export async function pMap<T, R>(
  items: readonly T[],
  fn: (item: T, index: number) => Promise<R>,
  concurrency = 4
): Promise<R[]> {
  const results: R[] = new Array(items.length);
  let cursor = 0;
  const workers = Array.from({ length: Math.min(concurrency, items.length) }, async () => {
    while (true) {
      const i = cursor++;
      if (i >= items.length) return;
      results[i] = await fn(items[i], i);
    }
  });
  await Promise.all(workers);
  return results;
}

export function downloadFile(name: string, content: string, mime = "text/csv"): void {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
