import type { Interval } from "./utils";

interface TimeframeToggleProps {
  value: Interval;
  onChange: (next: Interval) => void;
  options?: Interval[];
  className?: string;
}

const DEFAULT_OPTIONS: Interval[] = ["1m", "5m", "15m", "1h", "1d"];

const LABELS: Record<Interval, string> = {
  "1m": "1m",
  "5m": "5m",
  "15m": "15m",
  "1h": "1h",
  "1d": "1D",
};

/**
 * Accessible segmented control for chart interval.
 * Uses semantic <button> elements inside a role="tablist" container so screen
 * readers announce "selected" state correctly.
 */
export default function TimeframeToggle({
  value,
  onChange,
  options = DEFAULT_OPTIONS,
  className = "",
}: TimeframeToggleProps) {
  return (
    <div
      role="tablist"
      aria-label="Chart timeframe"
      className={`inline-flex items-center gap-0.5 rounded-md bg-zinc-900/60 p-0.5 ${className}`}
    >
      {options.map((opt) => {
        const selected = opt === value;
        return (
          <button
            key={opt}
            type="button"
            role="tab"
            aria-selected={selected}
            tabIndex={selected ? 0 : -1}
            onClick={() => {
              if (!selected) onChange(opt);
            }}
            className={`text-[10px] px-2 py-0.5 rounded font-medium transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-zinc-400 ${
              selected
                ? "bg-zinc-700 text-zinc-100"
                : "text-zinc-400 hover:text-zinc-200"
            }`}
          >
            {LABELS[opt]}
          </button>
        );
      })}
    </div>
  );
}
