import { useMemo } from "react";
import { formatINRPrecise } from "./utils";

export interface DepthLevel {
  price: number;
  qty: number;
}

interface DepthMiniProps {
  bids?: DepthLevel[];
  asks?: DepthLevel[];
  /** Number of levels per side to show. Defaults to 5. */
  levels?: number;
  className?: string;
}

/**
 * Compact bid/ask depth visualization. Each level shows price + qty with a
 * background bar proportional to that level's qty share within its side.
 *
 * Stub mode: if both sides are empty/undefined we render an unobtrusive
 * placeholder rather than nothing — the parent shouldn't have to special-case.
 */
export default function DepthMini({
  bids,
  asks,
  levels = 5,
  className = "",
}: DepthMiniProps) {
  const data = useMemo(() => {
    const b = (bids ?? []).slice(0, levels);
    const a = (asks ?? []).slice(0, levels);
    const maxBid = b.reduce((m, l) => Math.max(m, l.qty), 0) || 1;
    const maxAsk = a.reduce((m, l) => Math.max(m, l.qty), 0) || 1;
    return { b, a, maxBid, maxAsk };
  }, [bids, asks, levels]);

  const empty = data.b.length === 0 && data.a.length === 0;

  if (empty) {
    return (
      <div
        className={`text-[10px] text-zinc-600 italic px-2 py-1 ${className}`}
        aria-label="Order book depth unavailable"
      >
        Depth unavailable
      </div>
    );
  }

  return (
    <div
      className={`grid grid-cols-2 gap-1 text-[10px] font-mono ${className}`}
      aria-label="Order book depth, top levels"
    >
      <ul className="space-y-0.5" aria-label="Bids">
        {data.b.map((lvl, i) => {
          const w = (lvl.qty / data.maxBid) * 100;
          return (
            <li
              key={`b-${i}`}
              className="relative flex justify-between px-1.5 py-0.5 rounded-sm overflow-hidden"
            >
              <span
                aria-hidden="true"
                className="absolute inset-y-0 right-0 bg-emerald-500/15"
                style={{ width: `${w}%` }}
              />
              <span className="relative text-emerald-400">
                {formatINRPrecise(lvl.price)}
              </span>
              <span className="relative text-zinc-400">{lvl.qty}</span>
            </li>
          );
        })}
      </ul>
      <ul className="space-y-0.5" aria-label="Asks">
        {data.a.map((lvl, i) => {
          const w = (lvl.qty / data.maxAsk) * 100;
          return (
            <li
              key={`a-${i}`}
              className="relative flex justify-between px-1.5 py-0.5 rounded-sm overflow-hidden"
            >
              <span
                aria-hidden="true"
                className="absolute inset-y-0 left-0 bg-rose-500/15"
                style={{ width: `${w}%` }}
              />
              <span className="relative text-rose-400">
                {formatINRPrecise(lvl.price)}
              </span>
              <span className="relative text-zinc-400">{lvl.qty}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
