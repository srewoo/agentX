import { useEffect, useMemo, useState } from "react";
import { paperTrades, pMap } from "../../shared/localStore";
import { api } from "../../shared/api";
import type { PaperTrade } from "../../shared/types";

interface Props { onSelectSymbol?: (symbol: string) => void; }

function pnl(t: PaperTrade, last?: number): { value: number; pct: number } {
  const exit = t.status === "closed" ? (t.exit_price ?? t.entry_price) : (last ?? t.entry_price);
  const dir = t.side === "BUY" ? 1 : -1;
  const value = (exit - t.entry_price) * t.qty * dir;
  const pct = ((exit - t.entry_price) / t.entry_price) * 100 * dir;
  return { value, pct };
}

export default function PaperTrades({ onSelectSymbol }: Props) {
  const [trades, setTrades] = useState<PaperTrade[]>([]);
  const [quotes, setQuotes] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);

  // Manual entry form
  const [symbol, setSymbol] = useState("");
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [qty, setQty] = useState("");
  const [entry, setEntry] = useState("");
  const [target, setTarget] = useState("");
  const [stop, setStop] = useState("");

  const reload = async () => {
    setLoading(true);
    const list = await paperTrades.list();
    setTrades(list);
    const openSyms = Array.from(new Set(list.filter((t) => t.status === "open").map((t) => t.symbol)));
    const q: Record<string, number> = {};
    await pMap(openSyms, async (s) => {
      try {
        const r = await api.getQuote(s);
        if (r.price != null) q[s] = r.price;
      } catch { /* skip individual failures, don't blow up the page */ }
    }, 4);
    setQuotes(q);
    setLoading(false);
  };

  useEffect(() => { reload(); }, []);

  const open = useMemo(() => trades.filter((t) => t.status === "open"), [trades]);
  const closed = useMemo(() => trades.filter((t) => t.status === "closed").slice(0, 30), [trades]);

  const totalUnrealized = useMemo(() =>
    open.reduce((acc, t) => acc + pnl(t, quotes[t.symbol]).value, 0), [open, quotes]);
  const totalRealized = useMemo(() =>
    closed.reduce((acc, t) => acc + pnl(t).value, 0), [closed]);

  const closeTrade = async (id: string) => {
    const t = trades.find((x) => x.id === id);
    if (!t) return;
    const last = quotes[t.symbol] ?? t.entry_price;
    await paperTrades.update(id, { status: "closed", exit_price: last, exit_at: new Date().toISOString() });
    reload();
  };

  const removeTrade = async (id: string) => {
    await paperTrades.remove(id);
    reload();
  };

  const addManual = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!symbol.trim() || !qty || !entry) return;
    const t: PaperTrade = {
      id: crypto.randomUUID(),
      symbol: symbol.trim().toUpperCase(),
      side,
      qty: Number(qty),
      entry_price: Number(entry),
      entry_at: new Date().toISOString(),
      target: target ? Number(target) : undefined,
      stop_loss: stop ? Number(stop) : undefined,
      status: "open",
    };
    await paperTrades.add(t);
    setSymbol(""); setQty(""); setEntry(""); setTarget(""); setStop("");
    reload();
  };

  return (
    <div className="flex flex-col h-full">
      <div className="px-3 py-2 border-b border-border bg-zinc-900/40">
        <div className="flex items-center justify-around text-[11px]">
          <div className="text-center">
            <div className="text-zinc-500 text-[10px]">Open</div>
            <div className="text-zinc-100 font-bold">{open.length}</div>
          </div>
          <div className="text-center">
            <div className="text-zinc-500 text-[10px]">Unrealized</div>
            <div className={`font-bold ${totalUnrealized >= 0 ? "text-profit" : "text-loss"}`}>
              {totalUnrealized >= 0 ? "+" : ""}₹{totalUnrealized.toFixed(0)}
            </div>
          </div>
          <div className="text-center">
            <div className="text-zinc-500 text-[10px]">Realized (last 30)</div>
            <div className={`font-bold ${totalRealized >= 0 ? "text-profit" : "text-loss"}`}>
              {totalRealized >= 0 ? "+" : ""}₹{totalRealized.toFixed(0)}
            </div>
          </div>
        </div>
      </div>

      <form onSubmit={addManual} className="px-3 py-2 border-b border-border grid grid-cols-[1fr_60px_60px_60px_60px_60px_50px] gap-1 items-end">
        <input value={symbol} onChange={(e) => setSymbol(e.target.value)} placeholder="Symbol"
          className="bg-zinc-800 border border-border rounded px-1.5 py-1 text-[11px] text-zinc-100" />
        <select value={side} onChange={(e) => setSide(e.target.value as "BUY" | "SELL")}
          className="bg-zinc-800 border border-border rounded px-1 py-1 text-[11px] text-zinc-100">
          <option>BUY</option>
          <option>SELL</option>
        </select>
        <input value={qty} onChange={(e) => setQty(e.target.value)} type="number" placeholder="Qty"
          className="bg-zinc-800 border border-border rounded px-1.5 py-1 text-[11px] text-zinc-100" />
        <input value={entry} onChange={(e) => setEntry(e.target.value)} type="number" step="0.05" placeholder="Entry"
          className="bg-zinc-800 border border-border rounded px-1.5 py-1 text-[11px] text-zinc-100" />
        <input value={target} onChange={(e) => setTarget(e.target.value)} type="number" step="0.05" placeholder="Tgt"
          className="bg-zinc-800 border border-border rounded px-1.5 py-1 text-[11px] text-zinc-100" />
        <input value={stop} onChange={(e) => setStop(e.target.value)} type="number" step="0.05" placeholder="SL"
          className="bg-zinc-800 border border-border rounded px-1.5 py-1 text-[11px] text-zinc-100" />
        <button type="submit" className="bg-brand/20 border border-brand/30 text-brand-light rounded px-1.5 py-1 text-[10px] font-semibold">
          Add
        </button>
      </form>

      <div className="flex-1 overflow-y-auto px-3 py-2">
        {loading && <div className="text-xs text-zinc-500 text-center py-4">Loading…</div>}

        {!loading && open.length === 0 && closed.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full gap-2 text-zinc-500">
            <span className="text-3xl">📒</span>
            <p className="text-xs text-center">No paper trades yet. Add one above or click "Take this trade" on any signal.</p>
          </div>
        )}

        {open.length > 0 && (
          <>
            <div className="text-[10px] font-medium text-zinc-500 uppercase tracking-wider mb-1.5">
              Open ({open.length})
            </div>
            {open.map((t) => {
              const last = quotes[t.symbol];
              const p = pnl(t, last);
              return (
                <div key={t.id} className="border-b border-border/40 py-1.5 flex items-center gap-2 text-[11px]">
                  <button onClick={() => onSelectSymbol?.(t.symbol)} className="font-semibold text-zinc-100 hover:text-brand-light min-w-[60px] text-left">
                    {t.symbol}
                  </button>
                  <span className={`text-[10px] font-bold ${t.side === "BUY" ? "text-profit" : "text-loss"} min-w-[26px]`}>{t.side}</span>
                  <span className="text-zinc-500">{t.qty}@{t.entry_price.toFixed(1)}</span>
                  <span className="text-zinc-600">→ {last != null ? last.toFixed(1) : "—"}</span>
                  <span className={`ml-auto font-medium ${p.pct >= 0 ? "text-profit" : "text-loss"}`}>
                    {p.pct >= 0 ? "+" : ""}{p.pct.toFixed(1)}%
                  </span>
                  <button onClick={() => closeTrade(t.id)} className="text-[10px] px-1.5 py-0.5 rounded border border-border text-zinc-400 hover:text-zinc-100">
                    Close
                  </button>
                  <button onClick={() => removeTrade(t.id)} className="text-zinc-700 hover:text-loss text-sm">×</button>
                </div>
              );
            })}
          </>
        )}

        {closed.length > 0 && (
          <>
            <div className="text-[10px] font-medium text-zinc-500 uppercase tracking-wider mb-1.5 mt-3">
              Closed
            </div>
            {closed.map((t) => {
              const p = pnl(t);
              return (
                <div key={t.id} className="border-b border-border/40 py-1 flex items-center gap-2 text-[11px] opacity-80">
                  <span className="font-semibold text-zinc-300 min-w-[60px]">{t.symbol}</span>
                  <span className={`text-[10px] ${t.side === "BUY" ? "text-profit/80" : "text-loss/80"}`}>{t.side}</span>
                  <span className="text-zinc-600">{t.qty}@{t.entry_price.toFixed(1)} → {t.exit_price?.toFixed(1)}</span>
                  <span className={`ml-auto ${p.pct >= 0 ? "text-profit" : "text-loss"}`}>
                    {p.pct >= 0 ? "+" : ""}{p.pct.toFixed(1)}%
                  </span>
                  <button onClick={() => removeTrade(t.id)} className="text-zinc-700 hover:text-loss text-sm">×</button>
                </div>
              );
            })}
          </>
        )}
      </div>
    </div>
  );
}
