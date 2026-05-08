import { usePortfolio } from "../hooks/usePortfolio";
import { CardSkeleton } from "../layout/Skeleton";
import { EmptyState } from "../layout/EmptyState";
import { formatINRPrecise, formatPct, pctColorClass } from "../lib/format";

export default function PortfolioView() {
  const { data, isLoading, error } = usePortfolio();

  if (isLoading) {
    return (
      <div className="p-3 space-y-2">
        <CardSkeleton />
        <CardSkeleton />
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-3">
        <EmptyState icon="!" title="Couldn't load portfolio" body={error.message} />
      </div>
    );
  }

  if (!data || data.holdings.length === 0) {
    return (
      <div className="p-3">
        <EmptyState
          icon="◧"
          title="No holdings yet"
          body="Connect your broker or add a transaction to see your portfolio here."
        />
      </div>
    );
  }

  return (
    <div className="p-3 space-y-3">
      <div
        className="p-3 rounded-lg border"
        style={{ background: "var(--bg-panel)", borderColor: "var(--border-default)" }}
      >
        <div className="text-[11px] tk-text-muted">Market value</div>
        <div className="text-xl font-semibold tk-text">{formatINRPrecise(data.marketValue)}</div>
        <div className={`text-xs ${pctColorClass(data.pnlPct)}`}>
          {formatPct(data.pnlPct)} · {formatINRPrecise(data.pnl)}
        </div>
      </div>

      <ul className="space-y-2" aria-label="Holdings">
        {data.holdings.map((h) => (
          <li
            key={h.symbol}
            className="p-2.5 rounded-lg border flex items-center justify-between"
            style={{ background: "var(--bg-panel)", borderColor: "var(--border-default)" }}
          >
            <div className="min-w-0">
              <div className="text-sm font-semibold tk-text truncate">{h.symbol}</div>
              <div className="text-[11px] tk-text-muted truncate">{h.qty} @ {formatINRPrecise(h.avgPrice)}</div>
            </div>
            <div className="text-right">
              <div className="text-sm tk-text">{formatINRPrecise(h.marketValue)}</div>
              <div className={`text-[11px] ${pctColorClass(h.pnlPct)}`}>{formatPct(h.pnlPct)}</div>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
