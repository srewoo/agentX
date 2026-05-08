import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import type { SectorExposure } from "@/lib/types";
import { formatPct } from "@/lib/format";

const PALETTE = [
  "#34d399", "#60a5fa", "#f472b6", "#fbbf24", "#a78bfa",
  "#fb7185", "#22d3ee", "#facc15", "#94a3b8", "#fb923c",
];

interface SectorAllocationProps {
  data?: SectorExposure[];
}

/**
 * Donut chart of sector exposure with hover tooltips and a side legend.
 */
export default function SectorAllocation({ data: override }: SectorAllocationProps) {
  const [data, setData] = useState<SectorExposure[] | null>(override ?? null);
  const [error, setError] = useState<string | null>(null);
  const [hovered, setHovered] = useState<number | null>(null);

  useEffect(() => {
    if (override) {
      setData(override);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const res = await api.portfolio.sectorExposure();
        if (!cancelled) setData(res);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load sector exposure");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [override]);

  const arcs = useMemo(() => {
    if (!data || data.length === 0) return [];
    const total = data.reduce((s, d) => s + d.weight, 0) || 1;
    let acc = 0;
    return data.map((d, i) => {
      const frac = d.weight / total;
      const start = acc;
      acc += frac;
      return { ...d, frac, start, end: acc, color: PALETTE[i % PALETTE.length] };
    });
  }, [data]);

  if (error) {
    return (
      <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-300">
        {error}
      </div>
    );
  }

  if (!data) {
    return (
      <div aria-busy="true" className="flex h-44 animate-pulse items-center justify-center rounded-lg bg-neutral-800/40">
        <span className="text-xs text-neutral-500">Loading sector exposure…</span>
      </div>
    );
  }

  if (data.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-neutral-700 p-6 text-center text-sm text-neutral-400">
        No sector exposure to show yet.
      </div>
    );
  }

  return (
    <section aria-label="Sector allocation" className="rounded-xl border border-neutral-800 bg-neutral-900/40 p-3">
      <div className="text-[11px] uppercase tracking-wide text-neutral-400">Sector Allocation</div>
      <div className="mt-2 flex flex-col gap-3 sm:flex-row sm:items-center">
        <svg viewBox="0 0 100 100" className="h-32 w-32 shrink-0" role="img" aria-label="Sector allocation donut chart">
          {arcs.map((arc, i) => (
            <DonutSlice
              key={arc.sector}
              arc={arc}
              hovered={hovered === i}
              onEnter={() => setHovered(i)}
              onLeave={() => setHovered((h) => (h === i ? null : h))}
            />
          ))}
          {/* hole */}
          <circle cx="50" cy="50" r="22" fill="rgb(23 23 23)" />
          {hovered !== null && arcs[hovered] ? (
            <text x="50" y="48" textAnchor="middle" className="fill-neutral-200" style={{ fontSize: 8, fontWeight: 600 }}>
              {arcs[hovered].sector}
            </text>
          ) : (
            <text x="50" y="48" textAnchor="middle" className="fill-neutral-400" style={{ fontSize: 6 }}>
              Sectors
            </text>
          )}
          {hovered !== null && arcs[hovered] ? (
            <text x="50" y="58" textAnchor="middle" className="fill-neutral-400" style={{ fontSize: 7 }}>
              {formatPct(arcs[hovered].frac)}
            </text>
          ) : null}
        </svg>

        <ul className="flex-1 space-y-1 text-xs">
          {arcs.map((arc, i) => (
            <li
              key={arc.sector}
              className={`flex items-center justify-between rounded px-1 py-0.5 ${hovered === i ? "bg-neutral-800/60" : ""}`}
              onMouseEnter={() => setHovered(i)}
              onMouseLeave={() => setHovered((h) => (h === i ? null : h))}
            >
              <span className="flex items-center gap-2 text-neutral-300">
                <span aria-hidden="true" className="inline-block h-2 w-2 rounded-sm" style={{ background: arc.color }} />
                {arc.sector}
              </span>
              <span className="tabular-nums text-neutral-400">
                {formatPct(arc.frac)}
                {arc.dayChange != null && (
                  <span className={`ml-2 ${arc.dayChange >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
                    {arc.dayChange >= 0 ? "▲" : "▼"} {formatPct(Math.abs(arc.dayChange))}
                  </span>
                )}
              </span>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

function DonutSlice({
  arc,
  hovered,
  onEnter,
  onLeave,
}: {
  arc: { sector: string; frac: number; start: number; end: number; color: string };
  hovered: boolean;
  onEnter: () => void;
  onLeave: () => void;
}) {
  const r = 36;
  const cx = 50;
  const cy = 50;
  const TAU = Math.PI * 2;
  const a0 = arc.start * TAU - Math.PI / 2;
  const a1 = arc.end * TAU - Math.PI / 2;
  const x0 = cx + r * Math.cos(a0);
  const y0 = cy + r * Math.sin(a0);
  const x1 = cx + r * Math.cos(a1);
  const y1 = cy + r * Math.sin(a1);
  const largeArc = arc.frac > 0.5 ? 1 : 0;
  // Edge case: a single 100% slice cannot be drawn as an arc — render as a full circle.
  if (arc.frac >= 0.999) {
    return <circle cx={cx} cy={cy} r={r} fill={arc.color} opacity={hovered ? 1 : 0.85} />;
  }
  const d = `M ${cx} ${cy} L ${x0} ${y0} A ${r} ${r} 0 ${largeArc} 1 ${x1} ${y1} Z`;
  return (
    <path
      d={d}
      fill={arc.color}
      opacity={hovered ? 1 : 0.85}
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
      style={{ transition: "opacity 120ms" }}
    >
      <title>{`${arc.sector}: ${(arc.frac * 100).toFixed(1)}%`}</title>
    </path>
  );
}
