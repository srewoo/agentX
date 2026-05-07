import { useEffect, useMemo, useState } from "react";
import { api } from "../../shared/api";
import type { CorporateAction, WatchlistItem } from "../../shared/types";

interface Props {
  onSelectSymbol?: (symbol: string) => void;
}

const TYPE_STYLE: Record<string, string> = {
  Dividend: "bg-emerald-500/15 text-emerald-400 border-emerald-500/30",
  Split: "bg-blue-500/15 text-blue-400 border-blue-500/30",
  Bonus: "bg-purple-500/15 text-purple-400 border-purple-500/30",
  Earnings: "bg-amber-500/15 text-amber-400 border-amber-500/30",
  AGM: "bg-zinc-700/50 text-zinc-300 border-zinc-600/40",
};

function styleFor(t: string): string {
  const k = Object.keys(TYPE_STYLE).find((x) => t.toLowerCase().includes(x.toLowerCase()));
  return k ? TYPE_STYLE[k] : "bg-zinc-700/50 text-zinc-300 border-zinc-600/40";
}

function fmtDate(s?: string): string {
  if (!s) return "—";
  const d = new Date(s);
  if (isNaN(d.getTime())) return s;
  return d.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
}

export default function Earnings({ onSelectSymbol }: Props) {
  const [actions, setActions] = useState<CorporateAction[] | null>(null);
  const [watchlist, setWatchlist] = useState<WatchlistItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"ALL" | "WATCHLIST">("WATCHLIST");
  const [typeFilter, setTypeFilter] = useState<string>("ALL");

  useEffect(() => {
    api.getCorporateActions()
      .then((r) => setActions(r.actions || []))
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load"));
    api.getWatchlist().then((r) => setWatchlist(r.watchlist)).catch(() => {});
  }, []);

  const watchSet = useMemo(() => new Set(watchlist.map((w) => w.symbol.toUpperCase())), [watchlist]);

  const types = useMemo(() => {
    const set = new Set<string>();
    (actions ?? []).forEach((a) => set.add(a.action_type));
    return ["ALL", ...Array.from(set).sort()];
  }, [actions]);

  const filtered = useMemo(() => {
    let list = actions ?? [];
    if (filter === "WATCHLIST") list = list.filter((a) => watchSet.has(a.symbol?.toUpperCase()));
    if (typeFilter !== "ALL") list = list.filter((a) => a.action_type === typeFilter);
    return list.sort((a, b) => {
      const ta = a.ex_date ? Date.parse(a.ex_date) : Infinity;
      const tb = b.ex_date ? Date.parse(b.ex_date) : Infinity;
      return ta - tb;
    });
  }, [actions, watchSet, filter, typeFilter]);

  return (
    <div className="flex flex-col h-full">
      <div className="px-3 pt-2 pb-1.5 border-b border-border space-y-1.5">
        <div className="flex gap-1.5">
          {(["WATCHLIST", "ALL"] as const).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`text-[10px] font-semibold px-2 py-0.5 rounded-full border ${
                filter === f
                  ? "bg-brand/20 text-brand-light border-brand/40"
                  : "text-zinc-500 border-zinc-700 hover:text-zinc-300"
              }`}
            >
              {f === "WATCHLIST" ? "My watchlist" : "All NSE"}
            </button>
          ))}
          <span className="text-zinc-700 mx-0.5">|</span>
          <select
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value)}
            className="text-[10px] bg-zinc-800 border border-border rounded-full px-2 py-0.5 text-zinc-300"
          >
            {types.map((t) => <option key={t} value={t}>{t === "ALL" ? "All types" : t}</option>)}
          </select>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-2">
        {error && <div className="text-xs text-loss bg-loss/10 border border-loss/30 rounded p-2">{error}</div>}
        {!error && actions === null && <div className="text-xs text-zinc-500 text-center py-6">Loading…</div>}
        {!error && actions !== null && filtered.length === 0 && (
          <div className="flex flex-col items-center justify-center h-full gap-2 text-zinc-500">
            <span className="text-3xl">📅</span>
            <p className="text-xs">No upcoming corporate actions{filter === "WATCHLIST" ? " on your watchlist" : ""}.</p>
          </div>
        )}
        {filtered.map((a, i) => (
          <button
            key={`${a.symbol}-${a.action_type}-${i}`}
            onClick={() => a.symbol && onSelectSymbol?.(a.symbol)}
            className="w-full text-left flex items-start gap-2 py-2 border-b border-border/40 hover:bg-zinc-800/40 -mx-1 px-1 rounded"
          >
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2">
                <span className="font-semibold text-xs text-zinc-100">{a.symbol || "—"}</span>
                <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded border ${styleFor(a.action_type)}`}>
                  {a.action_type}
                </span>
              </div>
              {a.details && <div className="text-[11px] text-zinc-400 mt-0.5 line-clamp-2">{a.details}</div>}
            </div>
            <div className="text-right text-[10px] text-zinc-500 flex-shrink-0">
              <div>Ex {fmtDate(a.ex_date)}</div>
              {a.record_date && <div className="text-zinc-600">Rec {fmtDate(a.record_date)}</div>}
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}
