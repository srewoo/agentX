import { useEffect, useMemo, useState } from "react";
import { holdings as store, parseCSV, toCSV, downloadFile, pMap } from "../../shared/localStore";
import { api } from "../../shared/api";
import type { Holding, StockQuote } from "../../shared/types";

interface Props { onSelectSymbol?: (symbol: string) => void; }

export default function Holdings({ onSelectSymbol }: Props) {
  const [items, setItems] = useState<Holding[]>([]);
  const [quotes, setQuotes] = useState<Record<string, StockQuote>>({});
  const [loading, setLoading] = useState(true);

  // Manual entry form
  const [symbol, setSymbol] = useState("");
  const [qty, setQty] = useState("");
  const [avg, setAvg] = useState("");
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reload = async () => {
    setLoading(true);
    const list = await store.list();
    setItems(list);
    const q: Record<string, StockQuote> = {};
    await pMap(list, async (h) => {
      try { q[h.symbol] = await api.getQuote(h.symbol); } catch { /* skip */ }
    }, 4);
    setQuotes(q);
    setLoading(false);
  };

  useEffect(() => { reload(); }, []);

  const totals = useMemo(() => {
    let invested = 0, current = 0;
    items.forEach((h) => {
      invested += h.avg_price * h.qty;
      const last = quotes[h.symbol]?.price;
      current += (last ?? h.avg_price) * h.qty;
    });
    return { invested, current, pnl: current - invested, pct: invested > 0 ? ((current - invested) / invested) * 100 : 0 };
  }, [items, quotes]);

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!symbol.trim() || !qty || !avg) return;
    await store.upsert({
      symbol: symbol.trim().toUpperCase(),
      qty: Number(qty),
      avg_price: Number(avg),
    });
    setSymbol(""); setQty(""); setAvg("");
    reload();
  };

  const remove = async (s: string) => {
    await store.remove(s);
    reload();
  };

  const exportCSV = () => {
    const rows = items.map((h) => ({ symbol: h.symbol, qty: h.qty, avg_price: h.avg_price, notes: h.notes ?? "" }));
    downloadFile(`agentx-holdings-${new Date().toISOString().slice(0, 10)}.csv`, toCSV(rows));
  };

  const importCSV = async (file: File) => {
    setImporting(true);
    setError(null);
    try {
      const text = await file.text();
      const rows = parseCSV(text);
      if (!rows.length) throw new Error("Empty CSV");
      // Heuristic header mapping for Zerodha/Groww/Upstox exports
      const aliases = (r: Record<string, string>): Holding | null => {
        const sym = r.symbol || r.Symbol || r.Tradingsymbol || r["Trading Symbol"] || r["Stock"] || "";
        const qty = Number(r.qty || r.Qty || r.Quantity || r.quantity || r["Net Quantity"] || 0);
        const avg = Number(r.avg_price || r["Average Price"] || r["Avg. cost"] || r["Avg Price"] || r.average_price || 0);
        if (!sym || !qty || !avg) return null;
        return { symbol: String(sym).toUpperCase(), qty, avg_price: avg };
      };
      const parsed = rows.map(aliases).filter((x): x is Holding => x !== null);
      if (!parsed.length) throw new Error("Couldn't recognize headers. Need columns: symbol, qty, avg_price (or broker export).");
      // Merge with existing
      const existing = await store.list();
      const map = new Map<string, Holding>();
      [...existing, ...parsed].forEach((h) => map.set(h.symbol, h));
      await store.save(Array.from(map.values()));
      reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Import failed");
    } finally {
      setImporting(false);
    }
  };

  return (
    <div className="flex flex-col h-full">
      <div className="px-3 py-2 border-b border-border bg-zinc-900/40">
        <div className="flex items-center justify-between text-[11px]">
          <div>
            <span className="text-zinc-500">Invested </span>
            <span className="text-zinc-100 font-bold">₹{totals.invested.toLocaleString("en-IN", { maximumFractionDigits: 0 })}</span>
          </div>
          <div>
            <span className="text-zinc-500">Current </span>
            <span className="text-zinc-100 font-bold">₹{totals.current.toLocaleString("en-IN", { maximumFractionDigits: 0 })}</span>
          </div>
          <div>
            <span className="text-zinc-500">PnL </span>
            <span className={`font-bold ${totals.pnl >= 0 ? "text-profit" : "text-loss"}`}>
              {totals.pnl >= 0 ? "+" : ""}₹{Math.round(totals.pnl).toLocaleString("en-IN")} ({totals.pct >= 0 ? "+" : ""}{totals.pct.toFixed(1)}%)
            </span>
          </div>
        </div>
      </div>

      <div className="px-3 py-2 border-b border-border space-y-1.5">
        <form onSubmit={add} className="grid grid-cols-[1fr_70px_70px_50px] gap-1">
          <input value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="Symbol"
            className="bg-zinc-800 border border-border rounded px-1.5 py-1 text-[11px] text-zinc-100" />
          <input value={qty} onChange={(e) => setQty(e.target.value)} type="number" placeholder="Qty"
            className="bg-zinc-800 border border-border rounded px-1.5 py-1 text-[11px] text-zinc-100" />
          <input value={avg} onChange={(e) => setAvg(e.target.value)} type="number" step="0.05" placeholder="Avg ₹"
            className="bg-zinc-800 border border-border rounded px-1.5 py-1 text-[11px] text-zinc-100" />
          <button type="submit" className="bg-brand/20 border border-brand/30 text-brand-light rounded px-1.5 py-1 text-[10px] font-semibold">
            Add
          </button>
        </form>
        <div className="flex gap-1.5">
          <label className="flex-1 text-[10px] text-center px-2 py-1 rounded border border-border bg-zinc-800 text-zinc-300 hover:text-zinc-100 cursor-pointer">
            {importing ? "Importing…" : "Import CSV (broker export)"}
            <input type="file" accept=".csv" className="hidden" onChange={(e) => { const f = e.target.files?.[0]; if (f) importCSV(f); }} />
          </label>
          <button onClick={exportCSV} disabled={!items.length}
            className="text-[10px] px-2 py-1 rounded border border-border text-zinc-300 hover:text-zinc-100 disabled:opacity-40">
            Export CSV
          </button>
        </div>
        {error && <div className="text-[11px] text-loss">{error}</div>}
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-2">
        {loading && <div className="text-xs text-zinc-500 text-center py-4">Loading…</div>}
        {!loading && items.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full gap-2 text-zinc-500">
            <span className="text-3xl">💼</span>
            <p className="text-xs text-center max-w-[260px]">Add holdings manually or import a CSV from Zerodha / Groww / Upstox. Stays on your device.</p>
          </div>
        )}
        {items.map((h) => {
          const q = quotes[h.symbol];
          const last = q?.price ?? h.avg_price;
          const value = last * h.qty;
          const pnl = (last - h.avg_price) * h.qty;
          const pct = ((last - h.avg_price) / h.avg_price) * 100;
          return (
            <div key={h.symbol} className="grid grid-cols-[1fr_60px_60px_70px_24px] gap-2 items-center border-b border-border/40 py-1.5 text-[11px]">
              <button onClick={() => onSelectSymbol?.(h.symbol)} className="text-left">
                <div className="font-semibold text-zinc-100">{h.symbol}</div>
                <div className="text-[10px] text-zinc-500">{h.qty} @ ₹{h.avg_price.toFixed(1)}</div>
              </button>
              <div className="text-right text-zinc-300">{q?.price != null ? `₹${q.price.toFixed(1)}` : "—"}</div>
              <div className="text-right text-zinc-200">₹{Math.round(value).toLocaleString("en-IN")}</div>
              <div className={`text-right font-medium ${pnl >= 0 ? "text-profit" : "text-loss"}`}>
                {pnl >= 0 ? "+" : ""}{pct.toFixed(1)}%
              </div>
              <button onClick={() => remove(h.symbol)} className="text-zinc-700 hover:text-loss">×</button>
            </div>
          );
        })}
      </div>
    </div>
  );
}
