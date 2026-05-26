import { useEffect, useState } from "react";
import { api } from "../../shared/api";

type Options = Awaited<ReturnType<typeof api.getOptionsView>>;

/**
 * OptionsTab — per-symbol max-pain, PCR, IV, unusual activity.
 * Surfaces the previously-dormant options libraries on a real UI surface.
 */
export default function OptionsTab({ symbol }: { symbol: string }) {
  const [data, setData] = useState<Options | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const r = await api.getOptionsView(symbol);
        if (alive) setData(r);
      } catch (e) {
        if (alive) setErr(e instanceof Error ? e.message : "options fetch failed");
      }
    })();
    return () => { alive = false; };
  }, [symbol]);

  if (err) return <div className="text-[11px] text-red-400">{err}</div>;
  if (!data) return <div className="text-[11px] text-zinc-500">Loading options…</div>;

  const p = data.positioning;
  const dirColor = p.anchor_direction === "bullish" ? "text-emerald-400"
    : p.anchor_direction === "bearish" ? "text-red-400" : "text-zinc-300";

  return (
    <div className="space-y-2 text-[11px]">
      <div className="grid grid-cols-2 gap-2">
        <div className="border border-border rounded p-1.5 bg-zinc-900/40">
          <div className="text-zinc-500 uppercase tracking-wider text-[10px]">Max-pain anchor</div>
          <div className="text-zinc-200">{p.max_pain ?? "—"}</div>
          <div className="text-zinc-500 text-[10px]">
            spot {p.spot ?? "—"} · dist {p.distance_pct_to_max_pain ?? "—"}%
          </div>
          <div className={`text-[10px] ${dirColor}`}>{p.anchor_direction}</div>
        </div>
        <div className="border border-border rounded p-1.5 bg-zinc-900/40">
          <div className="text-zinc-500 uppercase tracking-wider text-[10px]">PCR (OI)</div>
          <div className="text-zinc-200">{p.pcr_oi ?? "—"}</div>
          <div className="text-zinc-500 text-[10px]">{p.pcr_signal ?? ""}</div>
        </div>
      </div>
      <div>
        <div className="text-zinc-500 uppercase tracking-wider text-[10px] mb-1">Unusual activity</div>
        {data.unusual_activity.length === 0 ? (
          <div className="text-zinc-500 text-[10px]">no flags</div>
        ) : (
          <table className="w-full">
            <thead className="text-zinc-500">
              <tr>
                <th className="text-left px-1 py-0.5 font-medium">Strike</th>
                <th className="text-right px-1 py-0.5 font-medium">OI</th>
                <th className="text-right px-1 py-0.5 font-medium">ΔOI</th>
                <th className="text-right px-1 py-0.5 font-medium">IV</th>
              </tr>
            </thead>
            <tbody>
              {data.unusual_activity.map((u, i) => (
                <tr key={i} className="border-t border-border/60">
                  <td className="px-1 py-0.5 text-zinc-300">{u.strike ?? "—"}</td>
                  <td className="px-1 py-0.5 text-right text-zinc-400">{u.oi ?? "—"}</td>
                  <td className="px-1 py-0.5 text-right text-zinc-400">{u.oi_change ?? "—"}</td>
                  <td className="px-1 py-0.5 text-right text-zinc-400">{u.iv ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
