import { useTheme } from "../theme/ThemeProvider";
import { useMarketStatus } from "../hooks/useMarketStatus";
import { marketStatusLabel } from "../lib/marketStatus";
import type { Exchange } from "../lib/types";

interface HeaderProps {
  exchange: Exchange;
  onExchangeChange: (e: Exchange) => void;
  onPopOut?: () => void;
  showPopOut: boolean;
}

export function Header({ exchange, onExchangeChange, onPopOut, showPopOut }: HeaderProps) {
  const { resolved, toggle } = useTheme();
  const status = useMarketStatus();

  const statusColor =
    status === "OPEN" ? "var(--color-profit)" :
    status === "PRE_OPEN" ? "var(--color-warn)" :
    status === "POST_CLOSE" ? "var(--text-secondary)" :
    "var(--text-muted)";

  return (
    <header
      className="flex-shrink-0 flex items-center justify-between gap-2 px-3 py-2 border-b"
      style={{
        borderBottomColor: "var(--border-default)",
        background: "var(--bg-panel)",
      }}
    >
      <div className="flex items-center gap-2 min-w-0">
        <span aria-hidden="true" style={{ color: "var(--accent-saffron)" }} className="text-lg">
          ◆
        </span>
        <span className="font-semibold text-sm tk-text">agentX</span>
        <span
          aria-label={`Market status: ${marketStatusLabel(status)}`}
          className="ml-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full border"
          style={{
            color: statusColor,
            borderColor: statusColor,
            background: "transparent",
          }}
        >
          {marketStatusLabel(status)}
        </span>
      </div>

      <div className="flex items-center gap-1.5">
        {/* Exchange toggle */}
        <div
          role="group"
          aria-label="Exchange"
          className="flex text-[10px] rounded overflow-hidden border"
          style={{ borderColor: "var(--border-default)" }}
        >
          {(["NSE", "BSE"] as Exchange[]).map((ex) => {
            const sel = exchange === ex;
            return (
              <button
                key={ex}
                type="button"
                aria-pressed={sel}
                onClick={() => onExchangeChange(ex)}
                className="px-2 py-1 transition-colors"
                style={{
                  background: sel ? "var(--accent-saffron-soft)" : "transparent",
                  color: sel ? "var(--accent-saffron)" : "var(--text-secondary)",
                  fontWeight: sel ? 600 : 400,
                }}
              >
                {ex}
              </button>
            );
          })}
        </div>

        <button
          type="button"
          onClick={toggle}
          aria-label={`Switch to ${resolved === "dark" ? "light" : "dark"} theme`}
          className="text-base leading-none px-1.5 py-1 rounded"
          style={{ color: "var(--text-secondary)" }}
        >
          {resolved === "dark" ? "☀" : "☾"}
        </button>

        {showPopOut && onPopOut && (
          <button
            type="button"
            onClick={onPopOut}
            aria-label="Open in standalone window"
            className="text-base leading-none px-1.5 py-1 rounded"
            style={{ color: "var(--text-secondary)" }}
          >
            ⛶
          </button>
        )}
      </div>
    </header>
  );
}
