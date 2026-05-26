import { useEffect, useState } from "react";
import { api } from "../../shared/api";

type Snap = Awaited<ReturnType<typeof api.getMarketSnapshot>>["data"];

function fmt(n: number | null | undefined, dp = 1): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return n.toFixed(dp);
}

function pct(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const s = n >= 0 ? "+" : "";
  return `${s}${n.toFixed(2)}%`;
}

/**
 * LiveMacroSnapshot — surface the macro tuple that's injected into every
 * LLM prompt. Two purposes:
 *   1. Lets the user see what the model is actually being told.
 *   2. Provides at-a-glance regime context on the Dashboard.
 */
export default function LiveMacroSnapshot() {
  const [snap, setSnap] = useState<Snap | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const r = await api.getMarketSnapshot();
        if (alive) setSnap(r.data);
      } catch (e) {
        if (alive) setErr(e instanceof Error ? e.message : "snapshot failed");
      }
    })();
    return () => { alive = false; };
  }, []);

  if (err) return <div className="text-[11px] text-red-400">Macro: {err}</div>;
  if (!snap) return <div className="text-[11px] text-zinc-500">Loading macro…</div>;

  return (
    <div className="border border-border rounded-md p-2 text-[11px] bg-zinc-900/40">
      <div className="flex justify-between items-center mb-1">
        <span className="text-zinc-400 font-semibold uppercase tracking-wider">Live macro</span>
        <span className="text-zinc-500">{snap.as_of}{snap.stale ? " · stale" : ""}</span>
      </div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-0.5">
        <span className="text-zinc-500">NIFTY 50</span>
        <span className="text-zinc-200 text-right">
          {fmt(snap.nifty_close, 1)} <span className={(snap.nifty_pct ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}>({pct(snap.nifty_pct)})</span>
        </span>
        <span className="text-zinc-500">Bank Nifty</span>
        <span className="text-zinc-200 text-right">
          {fmt(snap.bank_nifty_close, 1)} <span className={(snap.bank_nifty_pct ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}>({pct(snap.bank_nifty_pct)})</span>
        </span>
        <span className="text-zinc-500">India VIX</span>
        <span className="text-zinc-200 text-right">{fmt(snap.india_vix, 2)}</span>
        <span className="text-zinc-500">USD/INR</span>
        <span className="text-zinc-200 text-right">{fmt(snap.usd_inr, 2)}</span>
        <span className="text-zinc-500">Brent</span>
        <span className="text-zinc-200 text-right">${fmt(snap.brent_usd, 2)}</span>
        <span className="text-zinc-500">FII net</span>
        <span className="text-zinc-200 text-right">₹{fmt(snap.fii_net_cr, 0)} Cr</span>
        <span className="text-zinc-500">DII net</span>
        <span className="text-zinc-200 text-right">₹{fmt(snap.dii_net_cr, 0)} Cr</span>
      </div>
      {snap.sector_rotation && (
        <div className="mt-1 text-zinc-400 text-[10px] truncate">
          Rotation: {snap.sector_rotation}
        </div>
      )}
    </div>
  );
}
