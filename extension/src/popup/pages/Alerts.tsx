import { useState, useEffect, useCallback } from "react";
import { api } from "../../shared/api";

interface Alert {
  id: string;
  symbol: string;
  target_price: number;
  condition: string;
  current_price_at_creation: number | null;
  created_at: string;
  triggered_at: string | null;
  triggered_price: number | null;
  active: boolean;
  note: string | null;
}

export default function Alerts() {
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Form state
  const [symbol, setSymbol] = useState("");
  const [targetPrice, setTargetPrice] = useState("");
  const [condition, setCondition] = useState<"above" | "below">("above");
  const [note, setNote] = useState("");
  const [suggestions, setSuggestions] = useState<Array<{ symbol: string; name: string }>>([]);
  const [creating, setCreating] = useState(false);

  const loadAlerts = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.getAlerts();
      setAlerts(res.alerts);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load alerts");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadAlerts();
  }, [loadAlerts]);

  const handleSymbolInput = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const val = e.target.value.toUpperCase();
    setSymbol(val);
    if (val.length >= 1) {
      try {
        const res = await api.search(val);
        setSuggestions(res.results.slice(0, 5));
      } catch {
        setSuggestions([]);
      }
    } else {
      setSuggestions([]);
    }
  };

  const selectSuggestion = (sym: string) => {
    setSymbol(sym);
    setSuggestions([]);
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!symbol.trim() || !targetPrice.trim()) return;

    const price = parseFloat(targetPrice);
    if (isNaN(price) || price <= 0) {
      setError("Invalid target price");
      return;
    }

    setCreating(true);
    setError(null);
    try {
      await api.createAlert(symbol.trim(), price, condition, note.trim() || undefined);
      setSymbol("");
      setTargetPrice("");
      setNote("");
      setCondition("above");
      await loadAlerts();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to create alert");
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (alertId: string) => {
    try {
      await api.deleteAlert(alertId);
      setAlerts((prev) => prev.filter((a) => a.id !== alertId));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to delete alert");
    }
  };

  const activeAlerts = alerts.filter((a) => a.active);
  const triggeredAlerts = alerts.filter((a) => !a.active && a.triggered_at);

  const formatTime = (iso: string) => {
    const d = new Date(iso);
    return d.toLocaleDateString("en-IN", { day: "numeric", month: "short" }) +
      " " + d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });
  };

  return (
    <div className="flex flex-col h-full">
      {/* Create Alert Form */}
      <form onSubmit={handleCreate} className="px-3 pt-3 pb-2 border-b border-border space-y-2">
        <div className="flex gap-2">
          {/* Symbol input with autocomplete */}
          <div className="flex-1 relative">
            <input
              type="text"
              value={symbol}
              onChange={handleSymbolInput}
              placeholder="Symbol"
              className="w-full bg-zinc-800 border border-border rounded-lg px-2.5 py-1.5 text-xs text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-brand"
            />
            {suggestions.length > 0 && (
              <div className="absolute left-0 right-0 top-full mt-1 bg-zinc-800 border border-border rounded-lg z-10 overflow-hidden shadow-xl">
                {suggestions.map((s) => (
                  <button
                    key={s.symbol}
                    type="button"
                    onClick={() => selectSuggestion(s.symbol)}
                    className="w-full text-left px-2.5 py-1.5 text-xs hover:bg-zinc-700 flex items-center justify-between"
                  >
                    <span className="font-medium text-zinc-100">{s.symbol}</span>
                    <span className="text-zinc-500 text-[10px] truncate max-w-[120px]">{s.name}</span>
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Target price */}
          <input
            type="text"
            value={targetPrice}
            onChange={(e) => setTargetPrice(e.target.value)}
            placeholder="Target price"
            className="w-24 bg-zinc-800 border border-border rounded-lg px-2.5 py-1.5 text-xs text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-brand"
          />

          {/* Condition */}
          <select
            value={condition}
            onChange={(e) => setCondition(e.target.value as "above" | "below")}
            className="bg-zinc-800 border border-border rounded-lg px-2 py-1.5 text-xs text-zinc-100 focus:outline-none focus:border-brand"
          >
            <option value="above">Above</option>
            <option value="below">Below</option>
          </select>
        </div>

        <div className="flex gap-2">
          <input
            type="text"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Note (optional)"
            className="flex-1 bg-zinc-800 border border-border rounded-lg px-2.5 py-1.5 text-xs text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-brand"
          />
          <button
            type="submit"
            disabled={creating || !symbol.trim() || !targetPrice.trim()}
            className="bg-brand/20 text-brand-light border border-brand/30 px-3 py-1.5 rounded-lg text-xs font-medium hover:bg-brand/30 disabled:opacity-50"
          >
            {creating ? "..." : "Create"}
          </button>
        </div>
      </form>

      {/* Alert lists */}
      <div className="flex-1 overflow-y-auto px-3 py-2">
        {error && (
          <div className="text-xs text-loss bg-loss/10 border border-loss/30 rounded p-2 mb-2">
            {error}
          </div>
        )}

        {loading ? (
          <div className="text-xs text-zinc-500 text-center py-8">Loading alerts...</div>
        ) : (
          <>
            {/* Active Alerts */}
            {activeAlerts.length > 0 && (
              <div className="mb-4">
                <div className="text-[10px] font-medium text-zinc-500 uppercase tracking-wider mb-1.5">
                  Active Alerts ({activeAlerts.length})
                </div>
                {activeAlerts.map((alert) => (
                  <div
                    key={alert.id}
                    className="flex items-center justify-between py-2 border-b border-border/50"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="text-xs font-semibold text-zinc-100">{alert.symbol}</span>
                        <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium
                          ${alert.condition === "above"
                            ? "bg-profit/15 text-profit"
                            : "bg-loss/15 text-loss"
                          }`}>
                          {alert.condition === "above" ? ">" : "<"} {alert.target_price.toLocaleString("en-IN")}
                        </span>
                      </div>
                      <div className="flex items-center gap-2 mt-0.5">
                        <span className="text-[10px] text-zinc-500">{formatTime(alert.created_at)}</span>
                        {alert.note && (
                          <span className="text-[10px] text-zinc-600 truncate max-w-[160px]">{alert.note}</span>
                        )}
                      </div>
                    </div>
                    <button
                      onClick={() => handleDelete(alert.id)}
                      className="text-zinc-600 hover:text-loss text-sm leading-none ml-2 p-1"
                      title="Delete alert"
                    >
                      x
                    </button>
                  </div>
                ))}
              </div>
            )}

            {/* Triggered Alerts */}
            {triggeredAlerts.length > 0 && (
              <div className="mb-4">
                <div className="text-[10px] font-medium text-zinc-500 uppercase tracking-wider mb-1.5">
                  Triggered ({triggeredAlerts.length})
                </div>
                {triggeredAlerts.map((alert) => (
                  <div
                    key={alert.id}
                    className="flex items-center justify-between py-2 border-b border-border/50 opacity-70"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="text-xs font-semibold text-zinc-300">{alert.symbol}</span>
                        <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium
                          ${alert.condition === "above"
                            ? "bg-profit/10 text-profit/80"
                            : "bg-loss/10 text-loss/80"
                          }`}>
                          {alert.condition === "above" ? ">" : "<"} {alert.target_price.toLocaleString("en-IN")}
                        </span>
                        <span className="text-[10px] text-zinc-500">
                          triggered @ {alert.triggered_price?.toLocaleString("en-IN") ?? "—"}
                        </span>
                      </div>
                      <div className="flex items-center gap-2 mt-0.5">
                        <span className="text-[10px] text-zinc-600">
                          {alert.triggered_at ? formatTime(alert.triggered_at) : "—"}
                        </span>
                        {alert.note && (
                          <span className="text-[10px] text-zinc-600 truncate max-w-[160px]">{alert.note}</span>
                        )}
                      </div>
                    </div>
                    <button
                      onClick={() => handleDelete(alert.id)}
                      className="text-zinc-700 hover:text-loss text-sm leading-none ml-2 p-1"
                      title="Remove"
                    >
                      x
                    </button>
                  </div>
                ))}
              </div>
            )}

            {/* Empty state */}
            {activeAlerts.length === 0 && triggeredAlerts.length === 0 && (
              <div className="flex flex-col items-center justify-center h-full gap-3 text-zinc-500">
                <span className="text-4xl">🔔</span>
                <div className="text-center">
                  <p className="text-sm font-medium text-zinc-400">No price alerts</p>
                  <p className="text-xs mt-1">Create an alert above to get notified when a stock hits your target</p>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
