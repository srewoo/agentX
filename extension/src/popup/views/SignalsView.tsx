import { useState } from "react";
import { useRecommendations } from "../hooks/useRecommendations";
import { CardSkeleton } from "../layout/Skeleton";
import { EmptyState } from "../layout/EmptyState";
import type { Horizon, RecommendationFilters } from "../lib/types";

const HORIZONS: { id: Horizon; label: string }[] = [
  { id: "intraday", label: "Intraday" },
  { id: "swing", label: "Swing" },
  { id: "long", label: "Long" },
];

export default function SignalsView() {
  const [filters, setFilters] = useState<RecommendationFilters>({ horizon: "swing" });
  const { data, isLoading, error, refetch } = useRecommendations(filters);

  return (
    <div className="p-3 space-y-3">
      <div role="tablist" aria-label="Horizon" className="flex gap-1 text-xs">
        {HORIZONS.map((h) => {
          const sel = filters.horizon === h.id;
          return (
            <button
              key={h.id}
              role="tab"
              aria-selected={sel}
              onClick={() => setFilters((f) => ({ ...f, horizon: h.id }))}
              className="px-2.5 py-1 rounded-full transition-colors"
              style={{
                background: sel ? "var(--accent-saffron-soft)" : "var(--bg-panel)",
                color: sel ? "var(--accent-saffron)" : "var(--text-secondary)",
                border: `1px solid ${sel ? "var(--accent-saffron)" : "var(--border-default)"}`,
                fontWeight: sel ? 600 : 400,
              }}
            >
              {h.label}
            </button>
          );
        })}
      </div>

      {isLoading && (
        <div className="space-y-2">
          <CardSkeleton />
          <CardSkeleton />
          <CardSkeleton />
        </div>
      )}

      {!isLoading && error && (
        <EmptyState
          icon="!"
          title="Couldn't load recommendations"
          body={error.message}
          action={
            <button
              type="button"
              onClick={() => void refetch()}
              className="text-xs px-3 py-1.5 rounded"
              style={{ background: "var(--accent-saffron)", color: "#1a1a1a", fontWeight: 600 }}
            >
              Retry
            </button>
          }
        />
      )}

      {!isLoading && !error && (!data || data.length === 0) && (
        <EmptyState
          icon="∅"
          title="No signals match"
          body="Try a different horizon or relax the conviction filter to see more."
        />
      )}

      {!isLoading && !error && data && data.length > 0 && (
        <ul className="space-y-2" aria-label="Recommendations">
          {data.map((rec) => (
            <li
              key={rec.id}
              className="p-3 rounded-lg border"
              style={{ background: "var(--bg-panel)", borderColor: "var(--border-default)" }}
            >
              <div className="flex items-baseline justify-between gap-2">
                <div className="min-w-0">
                  <div className="text-sm font-semibold tk-text truncate">{rec.symbol}</div>
                  <div className="text-[11px] tk-text-muted truncate">{rec.name}</div>
                </div>
                <div
                  className="text-[10px] font-bold px-1.5 py-0.5 rounded"
                  style={{
                    background:
                      rec.direction === "BUY"
                        ? "var(--color-profit-soft)"
                        : rec.direction === "SELL"
                          ? "var(--color-loss-soft)"
                          : "var(--bg-panel-hover)",
                    color:
                      rec.direction === "BUY"
                        ? "var(--color-profit)"
                        : rec.direction === "SELL"
                          ? "var(--color-loss)"
                          : "var(--text-secondary)",
                  }}
                >
                  {rec.direction}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
