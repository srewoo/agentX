import { useState, useEffect, useCallback } from "react";
import { api } from "../../shared/api";
import type { WatchlistItem, StockQuote } from "../../shared/types";

export default function Watchlist() {
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([]);
  const [quotes, setQuotes] = useState<Record<string, StockQuote>>({});
  const [loading, setLoading] = useState(true);
  const [addQuery, setAddQuery] = useState("");
  const [suggestions, setSuggestions] = useState<Array<{ symbol: string; name: string }>>([]);
  const [error, setError] = useState<string | null>(null);

  const loadWatchlist = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.getWatchlist();
      setWatchlist(res.watchlist);
      // Fetch quotes for all watchlist symbols
      const quotePromises = res.watchlist.map((item) =>
        api.getQuote(item.symbol).then((q) => [item.symbol, q] as const).catch(() => null)
      );
      const results = await Promise.all(quotePromises);
      const qMap: Record<string, StockQuote> = {};
      results.forEach((r) => { if (r) qMap[r[0]] = r[1]; });
      setQuotes(qMap);
    } catch (e) {
      setError("Backend unavailable. Is the backend running?");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadWatchlist(); }, [loadWatchlist]);

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

      {/* List */}
      <div className="flex-1 overflow-y-auto px-3 py-2">
        {error && (
          <div className="text-xs text-loss bg-loss/10 border border-loss/30 rounded p-2 mb-2">
            {error}
          </div>
        )}

        {loading ? (
          <div className="text-xs text-zinc-500 text-center py-8">Loading watchlist...</div>
        ) : watchlist.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-full gap-3 text-zinc-500">
            <span className="text-4xl">★</span>
            <p className="text-sm text-center">Add stocks to your watchlist for priority signals</p>
          </div>
        ) : (
          watchlist.map((item) => {
            const q = quotes[item.symbol];
            const isPos = (q?.change_pct ?? 0) >= 0;
            return (
              <div key={item.symbol} className="flex items-center justify-between py-2.5 border-b border-border/50">
                <div>
                  <div className="font-semibold text-sm text-zinc-100">{item.symbol}</div>
                  <div className="text-xs text-zinc-500 truncate max-w-[180px]">{item.name}</div>
                </div>
                <div className="flex items-center gap-3">
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
