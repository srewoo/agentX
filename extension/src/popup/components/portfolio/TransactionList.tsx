import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { Transaction, TransactionsPage } from "@/lib/types";
import { formatINR, formatINRPrecise } from "@/lib/format";

interface TransactionListProps {
  /** Bumping this prop refreshes the first page (e.g. after a successful add). */
  refreshKey?: number;
  onAddClick?: () => void;
}

export default function TransactionList({ refreshKey = 0, onAddClick }: TransactionListProps) {
  const [items, setItems] = useState<Transaction[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadFirst = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res: TransactionsPage = await api.portfolio.transactions();
      setItems(res.data);
      setCursor(res.meta.nextCursor ?? null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load transactions");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadFirst();
  }, [loadFirst, refreshKey]);

  async function loadMore() {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const res = await api.portfolio.transactions(cursor);
      setItems((prev) => [...prev, ...res.data]);
      setCursor(res.meta.nextCursor ?? null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load more");
    } finally {
      setLoadingMore(false);
    }
  }

  if (loading) {
    return (
      <div aria-busy="true" className="space-y-2">
        {Array.from({ length: 5 }).map((_, i) => (
          <div key={i} className="h-9 animate-pulse rounded bg-neutral-800/60" />
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
        {error}
        <button
          type="button"
          onClick={() => void loadFirst()}
          className="ml-3 rounded border border-red-300/40 px-2 py-0.5 text-xs hover:bg-red-500/20"
        >
          Retry
        </button>
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-neutral-700 p-6 text-center text-sm text-neutral-400">
        <p>No transactions yet — record your first trade.</p>
        {onAddClick ? (
          <button
            type="button"
            onClick={onAddClick}
            className="mt-3 rounded-md bg-emerald-500/20 px-3 py-1.5 text-xs font-medium text-emerald-300 hover:bg-emerald-500/30"
          >
            Add transaction
          </button>
        ) : null}
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded-lg border border-neutral-800">
      <table className="w-full text-sm" aria-label="Transactions">
        <thead className="bg-neutral-900/60 text-[11px] uppercase tracking-wide text-neutral-400">
          <tr>
            <th scope="col" className="px-3 py-2 text-left">Date</th>
            <th scope="col" className="px-3 py-2 text-left">Symbol</th>
            <th scope="col" className="px-3 py-2 text-left">Side</th>
            <th scope="col" className="px-3 py-2 text-right">Qty</th>
            <th scope="col" className="px-3 py-2 text-right">Price</th>
            <th scope="col" className="px-3 py-2 text-right">Value</th>
          </tr>
        </thead>
        <tbody>
          {items.map((t) => (
            <tr key={t.id} className="border-t border-neutral-800">
              <td className="px-3 py-2 text-neutral-400">{formatDate(t.timestamp ?? t.executedAt)}</td>
              <td className="px-3 py-2">
                <div className="font-medium text-neutral-100">{t.symbol}</div>
                <div className="text-[10px] uppercase text-neutral-500">{t.exchange ?? "NSE"}</div>
              </td>
              <td className="px-3 py-2">
                <span
                  className={`rounded px-1.5 py-0.5 text-[10px] font-medium uppercase ${
                    String(t.side).toUpperCase() === "BUY"
                      ? "bg-emerald-500/15 text-emerald-300"
                      : "bg-rose-500/15 text-rose-300"
                  }`}
                >
                  {t.side}
                </span>
              </td>
              <td className="px-3 py-2 text-right tabular-nums">{t.qty}</td>
              <td className="px-3 py-2 text-right tabular-nums">{formatINRPrecise(t.price)}</td>
              <td className="px-3 py-2 text-right tabular-nums">{formatINR(t.qty * t.price)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {cursor ? (
        <div className="border-t border-neutral-800 p-2 text-center">
          <button
            type="button"
            onClick={() => void loadMore()}
            disabled={loadingMore}
            className="rounded-md px-3 py-1.5 text-xs font-medium text-neutral-300 hover:bg-neutral-800/60 disabled:opacity-50"
          >
            {loadingMore ? "Loading…" : "Load more"}
          </button>
        </div>
      ) : null}
    </div>
  );
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
}
