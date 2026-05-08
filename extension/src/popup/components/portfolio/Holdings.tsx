import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { useStreamQuote } from "@/lib/api";
import type { Holding } from "@/lib/types";
import { formatINR, formatINRPrecise, formatPct } from "@/lib/format";

type SortKey = "symbol" | "qty" | "ltp" | "marketValue" | "dayPnl" | "totalPnl";
type SortDir = "asc" | "desc";

interface HoldingsProps {
  /** Called when user clicks a holding row — host opens a chart tab for the symbol. */
  onOpenChart?: (h: Holding) => void;
}

/**
 * Sortable holdings table with live LTP streamed from `useStreamQuote`.
 */
export default function Holdings({ onOpenChart }: HoldingsProps) {
  const [rows, setRows] = useState<Holding[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>("marketValue");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await api.portfolio.holdings();
        if (!cancelled) setRows(res);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load holdings");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const sorted = useMemo(() => {
    if (!rows) return null;
    const copy = [...rows];
    copy.sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      const cmp = typeof av === "number" && typeof bv === "number"
        ? av - bv
        : String(av).localeCompare(String(bv));
      return sortDir === "asc" ? cmp : -cmp;
    });
    return copy;
  }, [rows, sortKey, sortDir]);

  function toggleSort(k: SortKey) {
    if (k === sortKey) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSortKey(k);
      setSortDir(k === "symbol" ? "asc" : "desc");
    }
  }

  if (error) {
    return (
      <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
        {error}
      </div>
    );
  }

  if (!sorted) {
    return (
      <div aria-busy="true" className="space-y-2" aria-label="Loading holdings">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="h-9 animate-pulse rounded bg-neutral-800/60" />
        ))}
      </div>
    );
  }

  if (sorted.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-neutral-700 p-6 text-center text-sm text-neutral-400">
        You hold no open positions.
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-neutral-800">
      <table className="w-full text-sm" aria-label="Open positions">
        <thead className="bg-neutral-900/60 text-neutral-400">
          <tr className="text-left text-[11px] uppercase tracking-wide">
            <Th k="symbol" label="Symbol" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} />
            <Th k="qty" label="Qty" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} align="right" />
            <Th k="ltp" label="LTP" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} align="right" />
            <Th k="marketValue" label="Mkt Value" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} align="right" />
            <Th k="dayPnl" label="Day P&L" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} align="right" />
            <Th k="totalPnl" label="Total P&L" sortKey={sortKey} sortDir={sortDir} onSort={toggleSort} align="right" />
          </tr>
        </thead>
        <tbody>
          {sorted.map((h) => (
            <HoldingRow key={`${h.exchange}:${h.symbol}`} holding={h} onOpenChart={onOpenChart} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Th({
  k,
  label,
  sortKey,
  sortDir,
  onSort,
  align = "left",
}: {
  k: SortKey;
  label: string;
  sortKey: SortKey;
  sortDir: SortDir;
  onSort: (k: SortKey) => void;
  align?: "left" | "right";
}) {
  const active = k === sortKey;
  return (
    <th scope="col" className={`px-3 py-2 ${align === "right" ? "text-right" : "text-left"}`}>
      <button
        type="button"
        onClick={() => onSort(k)}
        className="inline-flex items-center gap-1 font-medium text-inherit hover:text-neutral-200"
        aria-sort={active ? (sortDir === "asc" ? "ascending" : "descending") : "none"}
      >
        {label}
        {active ? <span aria-hidden="true">{sortDir === "asc" ? "▲" : "▼"}</span> : null}
      </button>
    </th>
  );
}

function HoldingRow({ holding, onOpenChart }: { holding: Holding; onOpenChart?: (h: Holding) => void }) {
  const liveMap = useStreamQuote([holding.symbol]);
  const live = liveMap[holding.symbol];
  // Fallbacks: live LTP > stored LTP > 0; never NaN-out the row.
  const ltp = live?.ltp ?? holding.ltp ?? 0;
  const marketValue = ltp * holding.qty;
  const baseLtp = holding.ltp ?? ltp;
  const livePnlDelta = (ltp - baseLtp) * holding.qty;
  const dayPnl = (holding.dayPnl ?? 0) + livePnlDelta;
  const totalPnl = (holding.totalPnl ?? holding.pnl ?? 0) + livePnlDelta;
  const dayUp = dayPnl >= 0;
  const totalUp = totalPnl >= 0;

  function handleClick() {
    onOpenChart?.(holding);
  }

  function handleKey(e: React.KeyboardEvent<HTMLTableRowElement>) {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onOpenChart?.(holding);
    }
  }

  return (
    <tr
      tabIndex={onOpenChart ? 0 : -1}
      role={onOpenChart ? "link" : undefined}
      onClick={onOpenChart ? handleClick : undefined}
      onKeyDown={onOpenChart ? handleKey : undefined}
      aria-label={onOpenChart ? `Open chart for ${holding.symbol}` : undefined}
      className={`border-t border-neutral-800 ${onOpenChart ? "cursor-pointer hover:bg-neutral-900/40 focus:bg-neutral-900/60 focus:outline-none focus:ring-1 focus:ring-emerald-500/40" : ""}`}
    >
      <td className="px-3 py-2">
        <div className="font-medium text-neutral-100">{holding.symbol}</div>
        <div className="text-[10px] uppercase text-neutral-500">{holding.exchange} · {holding.sector}</div>
      </td>
      <td className="px-3 py-2 text-right tabular-nums">{holding.qty}</td>
      <td className="px-3 py-2 text-right tabular-nums">{formatINRPrecise(ltp)}</td>
      <td className="px-3 py-2 text-right tabular-nums">{formatINR(marketValue)}</td>
      <td className={`px-3 py-2 text-right tabular-nums ${dayUp ? "text-emerald-400" : "text-rose-400"}`}>
        {dayUp ? "↑" : "↓"} {formatINRPrecise(Math.abs(dayPnl))}
      </td>
      <td className={`px-3 py-2 text-right tabular-nums ${totalUp ? "text-emerald-400" : "text-rose-400"}`}>
        {totalUp ? "+" : "-"}{formatINRPrecise(Math.abs(totalPnl))}
        <span className="ml-1 text-xs text-neutral-500">
          ({formatPct((totalPnl / (holding.avgPrice * holding.qty)) || 0)})
        </span>
      </td>
    </tr>
  );
}
