import { useState, useEffect, useCallback, useMemo } from "react";
import { api } from "../../shared/api";
import { watchlistGroups, parseCSV, toCSV, downloadFile, pMap } from "../../shared/localStore";
import type { WatchlistItem, StockQuote, WatchlistGroup } from "../../shared/types";

interface WatchlistProps { onSelectSymbol?: (symbol: string) => void; }

export default function Watchlist({ onSelectSymbol }: WatchlistProps = {}) {
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([]);
  const [quotes, setQuotes] = useState<Record<string, StockQuote>>({});
  const [loading, setLoading] = useState(true);
  const [addQuery, setAddQuery] = useState("");
  const [suggestions, setSuggestions] = useState<Array<{ symbol: string; name: string }>>([]);
  const [error, setError] = useState<string | null>(null);
  const [groups, setGroups] = useState<WatchlistGroup[]>([]);
  const [activeGroup, setActiveGroup] = useState<string>("All");
  const [importing, setImporting] = useState(false);

  const groupMap = useMemo(() => {
    const m = new Map<string, string>();
    groups.forEach((g) => m.set(g.symbol, g.group));
    return m;
  }, [groups]);

  const groupNames = useMemo(() => {
    const names = new Set<string>(["All", "Default"]);
    groups.forEach((g) => names.add(g.group));
    return Array.from(names);
  }, [groups]);

  const loadWatchlist = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.getWatchlist();
      setWatchlist(res.watchlist);
      // Fetch quotes for all watchlist symbols with a concurrency cap so we
      // don't fire 50 parallel quote calls (which then cascade to NSE/yfinance).
      const qMap: Record<string, StockQuote> = {};
      await pMap(res.watchlist, async (item) => {
        try {
          const ex = (item.exchange === "BSE" ? "BSE" : "NSE") as "NSE" | "BSE";
          qMap[item.symbol] = await api.getQuote(item.symbol, ex);
        } catch { /* skip */ }
      }, 4);
      setQuotes(qMap);
    } catch (e) {
      setError("Backend unavailable. Is the backend running?");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadWatchlist(); }, [loadWatchlist]);
  useEffect(() => { watchlistGroups.list().then(setGroups); }, []);

  const setSymbolGroup = async (symbol: string, group: string) => {
    await watchlistGroups.setGroup(symbol, group);
    setGroups(await watchlistGroups.list());
  };

  const exportCSV = () => {
    const rows = watchlist.map((w) => ({
      symbol: w.symbol, name: w.name, exchange: w.exchange, group: groupMap.get(w.symbol) ?? "Default",
    }));
    downloadFile(`agentx-watchlist-${new Date().toISOString().slice(0, 10)}.csv`, toCSV(rows));
  };

  const importCSV = async (file: File) => {
    setImporting(true); setError(null);
    try {
      const text = await file.text();
      const rows = parseCSV(text);
      if (!rows.length) throw new Error("Empty CSV");
      const seen = new Set(watchlist.map((w) => w.symbol.toUpperCase()));
      let added = 0;
      for (const r of rows) {
        const sym = (r.symbol || r.Symbol || r.Tradingsymbol || r["Trading Symbol"] || r.Stock || "").toString().toUpperCase().trim();
        if (!sym || seen.has(sym)) continue;
        const name = (r.name || r.Name || r.Company || sym).toString();
        const grp = (r.group || r.Group || "Default").toString();
        try {
          await api.addToWatchlist(sym, name);
          await watchlistGroups.setGroup(sym, grp);
          added++;
          seen.add(sym);
        } catch { /* skip individual failures */ }
      }
      await loadWatchlist();
      setGroups(await watchlistGroups.list());
      if (added === 0) setError("No new symbols imported. Need column 'symbol' (and optionally 'name', 'group').");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Import failed");
    } finally {
      setImporting(false);
    }
  };

  const filteredWatchlist = useMemo(() => {
    if (activeGroup === "All") return watchlist;
    return watchlist.filter((w) => (groupMap.get(w.symbol) ?? "Default") === activeGroup);
  }, [watchlist, activeGroup, groupMap]);

  const handleAddInput = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const val = e.target.value;
    setAddQuery(val);
    if (val.length >= 1) {
      const res = await api.search(val).catch(() => ({ results: [] }));
      setSuggestions(res.results.slice(0, 5));
    } else {
      setSuggestions([]);
    }
  };

  const addStock = async (symbol: string, name: string) => {
    setError(null);
    try {
      await api.addToWatchlist(symbol, name);
      setAddQuery("");
      setSuggestions([]);
      await loadWatchlist();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to add");
    }
  };

  const removeStock = async (symbol: string) => {
    try {
      await api.removeFromWatchlist(symbol);
      setWatchlist((prev) => prev.filter((s) => s.symbol !== symbol));
      setQuotes((prev) => { const n = { ...prev }; delete n[symbol]; return n; });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to remove");
    }
  };

  return (
    <div className="flex flex-col h-full">
      {/* Add input */}
      <div className="px-3 pt-3 pb-2 relative border-b border-border">
        <input
          type="text"
          value={addQuery}
          onChange={handleAddInput}
          placeholder="Add stock to watchlist..."
          className="w-full bg-zinc-800 border border-border rounded-lg px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-brand"
        />
        {suggestions.length > 0 && (
          <div className="absolute left-3 right-3 top-full mt-1 bg-zinc-800 border border-border rounded-lg z-10 overflow-hidden shadow-xl">
            {suggestions.map((s) => (
              <button
                key={s.symbol}
                onClick={() => addStock(s.symbol, s.name)}
                className="w-full text-left px-3 py-2 text-sm hover:bg-zinc-700 flex items-center justify-between"
              >
                <span className="font-medium text-zinc-100">{s.symbol}</span>
                <span className="text-zinc-500 text-xs truncate max-w-[180px]">{s.name}</span>
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Groups + CSV */}
      <div className="px-3 pt-2 pb-1.5 border-b border-border flex items-center gap-1.5 overflow-x-auto">
        {groupNames.map((g) => (
          <button
            key={g}
            onClick={() => setActiveGroup(g)}
            className={`text-[10px] font-semibold px-2 py-0.5 rounded-full border whitespace-nowrap ${
              activeGroup === g ? "bg-brand/20 text-brand-light border-brand/40" : "text-zinc-500 border-zinc-700 hover:text-zinc-300"
            }`}
          >
            {g}
          </button>
        ))}
        <span className="ml-auto flex gap-1.5">
          <label className="text-[10px] px-1.5 py-0.5 rounded border border-border text-zinc-300 hover:text-zinc-100 cursor-pointer whitespace-nowrap">
            {importing ? "…" : "Import"}
            <input type="file" accept=".csv" className="hidden" onChange={(e) => { const f = e.target.files?.[0]; if (f) importCSV(f); }} />
          </label>
          <button onClick={exportCSV} disabled={!watchlist.length}
            className="text-[10px] px-1.5 py-0.5 rounded border border-border text-zinc-300 hover:text-zinc-100 disabled:opacity-40">
            Export
          </button>
        </span>
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto px-3 py-2">
        {error && (
          <div className="text-xs text-loss bg-loss/10 border border-loss/30 rounded p-2 mb-2">
            {error}
          </div>
        )}

        {loading ? (
          <div className="text-xs text-zinc-500 text-center py-8">Loading watchlist...</div>
        ) : filteredWatchlist.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full gap-3 text-zinc-500">
            <span className="text-4xl">★</span>
            <p className="text-sm text-center">
              {watchlist.length === 0
                ? "Add stocks to your watchlist for priority signals"
                : `No items in group "${activeGroup}"`}
            </p>
          </div>
        ) : (
          filteredWatchlist.map((item) => {
            const q = quotes[item.symbol];
            const isPos = (q?.change_pct ?? 0) >= 0;
            const grp = groupMap.get(item.symbol) ?? "Default";
            return (
              <div key={item.symbol} className="flex items-center justify-between py-2 border-b border-border/50">
                <button
                  onClick={() => onSelectSymbol?.(item.symbol)}
                  className="text-left flex-1 min-w-0"
                >
                  <div className="font-semibold text-sm text-zinc-100">{item.symbol}</div>
                  <div className="text-xs text-zinc-500 truncate max-w-[180px]">{item.name}</div>
                </button>
                <div className="flex items-center gap-2">
                  {q ? (
                    <div className="text-right">
                      <div className="text-sm font-medium text-zinc-100">
                        {q.price != null ? `₹${q.price.toLocaleString("en-IN")}` : "—"}
                      </div>
                      {q.change_pct != null && (
                        <div className={`text-xs ${isPos ? "text-profit" : "text-loss"}`}>
                          {isPos ? "▲" : "▼"} {Math.abs(q.change_pct).toFixed(2)}%
                        </div>
                      )}
                    </div>
                  ) : (
                    <div className="text-xs text-zinc-600">—</div>
                  )}
                  <select
                    value={grp}
                    onChange={(e) => setSymbolGroup(item.symbol, e.target.value)}
                    onClick={(e) => e.stopPropagation()}
                    className="text-[10px] bg-zinc-800 border border-border rounded px-1 py-0.5 text-zinc-300 max-w-[80px]"
                    title="Group"
                  >
                    {groupNames.filter((n) => n !== "All").map((g) => <option key={g} value={g}>{g}</option>)}
                    <option value="__new__" disabled>──</option>
                  </select>
                  <button
                    onClick={async () => {
                      const name = prompt("New group name", "Watchlist 2");
                      if (name) await setSymbolGroup(item.symbol, name.trim());
                    }}
                    className="text-zinc-600 hover:text-zinc-300 text-xs"
                    title="New group"
                  >+</button>
                  <button
                    onClick={() => removeStock(item.symbol)}
                    className="text-zinc-600 hover:text-loss text-lg leading-none"
                    title="Remove"
                  >
                    ×
                  </button>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}
