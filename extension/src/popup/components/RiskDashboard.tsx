import { useEffect, useState } from "react";
import { api } from "../../shared/api";

type Risk = Awaited<ReturnType<typeof api.getRiskDashboard>>;

/**
 * RiskDashboard — sector exposure + pairwise correlation heatmap for the
 * open paper-trade book. Highlights the 0.7+ correlation clusters and
 * >25% sector concentration that the auto-trader's risk gate also enforces.
 */
export default function RiskDashboard() {
  const [data, setData] = useState<Risk | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const r = await api.getRiskDashboard();
        if (alive) setData(r);
      } catch (e) {
        if (alive) setErr(e instanceof Error ? e.message : "risk dashboard failed");
      }
    })();
    return () => { alive = false; };
  }, []);

  if (err) return <div className="text-[11px] text-red-400">{err}</div>;
  if (!data) return <div className="text-[11px] text-zinc-500">Loading risk dashboard…</div>;

  return (
    <div className="space-y-2 text-[11px]">
      {data.alerts.length > 0 && (
        <div className="space-y-1">
          {data.alerts.map((a, i) => (
            <div key={i} className={`px-2 py-1 rounded border text-[11px] ${
              a.severity === "warn" ? "border-amber-700 bg-amber-950/30 text-amber-300"
                : "border-zinc-700 bg-zinc-900 text-zinc-300"
            }`}>
              <span className="uppercase tracking-wider text-[10px] mr-1.5">{a.kind}</span>
              {a.message}
            </div>
          ))}
        </div>
      )}
      <div>
        <div className="text-zinc-500 uppercase tracking-wider text-[10px] mb-1">Open positions ({data.open_positions.length})</div>
        {data.open_positions.length === 0 ? (
          <div className="text-zinc-500">no open positions</div>
        ) : (
          <table className="w-full">
            <thead className="text-zinc-500">
              <tr>
                <th className="text-left px-1 py-0.5 font-medium">Symbol</th>
                <th className="text-left px-1 py-0.5 font-medium">Dir</th>
                <th className="text-right px-1 py-0.5 font-medium">Entry</th>
                <th className="text-right px-1 py-0.5 font-medium">SL</th>
                <th className="text-right px-1 py-0.5 font-medium">Target</th>
              </tr>
            </thead>
            <tbody>
              {data.open_positions.map((p, i) => (
                <tr key={i} className="border-t border-border/60">
                  <td className="px-1 py-0.5 text-zinc-300">{String(p.symbol)}</td>
                  <td className="px-1 py-0.5 text-zinc-400">{String(p.direction)}</td>
                  <td className="px-1 py-0.5 text-right text-zinc-400">{String(p.entry_price)}</td>
                  <td className="px-1 py-0.5 text-right text-zinc-400">{p.stop_loss ? String(p.stop_loss) : "—"}</td>
                  <td className="px-1 py-0.5 text-right text-zinc-400">{p.target ? String(p.target) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {data.correlation_matrix.length > 0 && (
        <div>
          <div className="text-zinc-500 uppercase tracking-wider text-[10px] mb-1">Correlations</div>
          <table className="w-full">
            <thead className="text-zinc-500">
              <tr>
                <th className="text-left px-1 py-0.5 font-medium">Symbol</th>
                <th className="text-right px-1 py-0.5 font-medium">Max corr</th>
                <th className="text-left px-1 py-0.5 font-medium">With</th>
              </tr>
            </thead>
            <tbody>
              {data.correlation_matrix.map((m, i) => (
                <tr key={i} className="border-t border-border/60">
                  <td className="px-1 py-0.5 text-zinc-300">{m.symbol}</td>
                  <td className={`px-1 py-0.5 text-right ${
                    (m.max_correlation ?? 0) >= 0.7 ? "text-amber-300" : "text-zinc-400"
                  }`}>
                    {m.max_correlation == null ? "—" : m.max_correlation.toFixed(2)}
                  </td>
                  <td className="px-1 py-0.5 text-zinc-500">{m.most_correlated_with ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
