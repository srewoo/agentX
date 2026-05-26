import { useEffect, useState } from "react";
import { api } from "../../shared/api";

type Cohort = Awaited<ReturnType<typeof api.getCohort>>;

/**
 * CohortPanel — "Since rule change" view. The default floor is 2026-05-26
 * (post conviction overhaul). Surfaces WR / Wilson lower bound / sample
 * size per (signal_type, direction) so a high WR on n=10 looks honest.
 */
export default function CohortPanel() {
  const [since, setSince] = useState("2026-05-26");
  const [data, setData] = useState<Cohort | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    (async () => {
      try {
        const r = await api.getCohort(since);
        if (alive) setData(r);
      } catch (e) {
        if (alive) setErr(e instanceof Error ? e.message : "cohort failed");
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, [since]);

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <label className="text-[11px] text-zinc-400">Since</label>
        <input
          type="date"
          value={since}
          onChange={(e) => setSince(e.target.value)}
          className="bg-zinc-900 border border-border rounded px-1 py-0.5 text-[11px] text-zinc-200"
        />
      </div>
      {err && <div className="text-[11px] text-red-400">{err}</div>}
      {loading && <div className="text-[11px] text-zinc-500">Loading cohort…</div>}
      {data && (
        <>
          <div className="grid grid-cols-3 gap-2 text-[11px]">
            <div className="border border-border rounded p-1.5 bg-zinc-900/40">
              <div className="text-zinc-500 uppercase tracking-wider text-[10px]">Signals WR</div>
              <div className="text-zinc-200 text-base">{data.signals.totals.win_rate.toFixed(1)}%</div>
              <div className="text-zinc-500 text-[10px]">Wilson LB {data.signals.totals.wilson_lb.toFixed(1)}% · n={data.signals.totals.wins + data.signals.totals.losses}</div>
            </div>
            <div className="border border-border rounded p-1.5 bg-zinc-900/40">
              <div className="text-zinc-500 uppercase tracking-wider text-[10px]">Recos WR</div>
              <div className="text-zinc-200 text-base">{data.recommendations.win_rate.toFixed(1)}%</div>
              <div className="text-zinc-500 text-[10px]">Wilson LB {data.recommendations.wilson_lb.toFixed(1)}% · n={data.recommendations.wins + data.recommendations.losses}</div>
            </div>
            <div className="border border-border rounded p-1.5 bg-zinc-900/40">
              <div className="text-zinc-500 uppercase tracking-wider text-[10px]">Considered HOLDs</div>
              <div className="text-zinc-200 text-base">{data.recommendations.considered_holds}</div>
              <div className="text-zinc-500 text-[10px]">engine looked but didn't act</div>
            </div>
          </div>
          <div className="border border-border rounded-md overflow-hidden">
            <table className="w-full text-[11px]">
              <thead className="bg-zinc-900/60 text-zinc-500">
                <tr>
                  <th className="text-left px-2 py-1 font-medium">Signal</th>
                  <th className="text-left px-2 py-1 font-medium">Dir</th>
                  <th className="text-right px-2 py-1 font-medium">n</th>
                  <th className="text-right px-2 py-1 font-medium">WR</th>
                  <th className="text-right px-2 py-1 font-medium">Wilson LB</th>
                  <th className="text-right px-2 py-1 font-medium">Avg PnL</th>
                </tr>
              </thead>
              <tbody>
                {data.signals.by_type.map((r, i) => (
                  <tr key={`${r.signal_type}-${r.direction}-${i}`} className="border-t border-border/60">
                    <td className="px-2 py-1 text-zinc-300 truncate max-w-[140px]">{r.signal_type}</td>
                    <td className="px-2 py-1 text-zinc-400">{r.direction}</td>
                    <td className="px-2 py-1 text-right text-zinc-400">{r.wins + r.losses}</td>
                    <td className="px-2 py-1 text-right text-zinc-200">{r.win_rate.toFixed(1)}%</td>
                    <td className="px-2 py-1 text-right text-zinc-500">{r.wilson_lb.toFixed(1)}%</td>
                    <td className={`px-2 py-1 text-right ${r.avg_pnl_pct > 0 ? "text-emerald-400" : r.avg_pnl_pct < 0 ? "text-red-400" : "text-zinc-400"}`}>
                      {(r.avg_pnl_pct >= 0 ? "+" : "") + r.avg_pnl_pct.toFixed(2)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
