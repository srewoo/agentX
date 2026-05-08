import { useMemo } from "react";
import type { Holding, SectorExposure } from "@/lib/types";
import { formatPct } from "@/lib/format";

interface RiskBadgesProps {
  holdings: Holding[];
  sectorExposure: SectorExposure[];
  /** Symbols flagged as illiquid by the backend or screener. */
  illiquidSymbols?: string[];
  /** Symbols missing a stop-loss in the user's risk plan. */
  symbolsWithoutSL?: string[];
}

interface Badge {
  id: string;
  severity: "warn" | "danger";
  label: string;
  detail: string;
}

/**
 * Surfaces concentration / sector / SL / liquidity risks.
 * Renders nothing when no risks fire.
 */
export default function RiskBadges({
  holdings,
  sectorExposure,
  illiquidSymbols = [],
  symbolsWithoutSL = [],
}: RiskBadgesProps) {
  const badges = useMemo<Badge[]>(() => {
    const out: Badge[] = [];
    const totalMv = holdings.reduce((s, h) => s + (h.marketValue ?? 0), 0);

    if (totalMv > 0) {
      for (const h of holdings) {
        const w = (h.marketValue ?? 0) / totalMv;
        if (w > 0.2) {
          out.push({
            id: `concentration-${h.symbol}`,
            severity: w > 0.35 ? "danger" : "warn",
            label: `${h.symbol} ${formatPct(w)}`,
            detail: `Single-name concentration above 20%.`,
          });
        }
      }
    }

    for (const s of sectorExposure) {
      if (s.weight > 0.35) {
        out.push({
          id: `sector-${s.sector}`,
          severity: s.weight > 0.5 ? "danger" : "warn",
          label: `${s.sector} ${formatPct(s.weight)}`,
          detail: `Sector exposure above 35%.`,
        });
      }
    }

    for (const sym of symbolsWithoutSL) {
      out.push({
        id: `no-sl-${sym}`,
        severity: "warn",
        label: `${sym} • no SL`,
        detail: `No stop-loss set for this position.`,
      });
    }

    for (const sym of illiquidSymbols) {
      out.push({
        id: `illiquid-${sym}`,
        severity: "warn",
        label: `${sym} • illiquid`,
        detail: `Low average daily volume — exits may be slow.`,
      });
    }

    return out;
  }, [holdings, sectorExposure, illiquidSymbols, symbolsWithoutSL]);

  if (badges.length === 0) return null;

  return (
    <section aria-label="Portfolio risk warnings" className="flex flex-wrap items-center gap-2">
      <span className="text-[11px] uppercase tracking-wide text-neutral-400">Risk</span>
      <ul className="flex flex-wrap gap-1.5">
        {badges.map((b) => (
          <li key={b.id}>
            <span
              role="status"
              title={b.detail}
              className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium ${
                b.severity === "danger"
                  ? "border-rose-500/40 bg-rose-500/15 text-rose-300"
                  : "border-amber-500/40 bg-amber-500/15 text-amber-300"
              }`}
            >
              <span aria-hidden="true">{b.severity === "danger" ? "⚠" : "!"}</span>
              {b.label}
              <span className="sr-only"> — {b.detail}</span>
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}
