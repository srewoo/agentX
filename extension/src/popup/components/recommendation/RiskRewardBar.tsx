import { formatINRPrecise } from "@/lib/format";

export interface RiskRewardBarProps {
  stoploss: number;
  entry: number;
  target1: number;
  target2?: number;
  /** 'BUY' bars run left→right with green target zone; 'SELL' bars are inverted. */
  direction?: "long" | "short";
  className?: string;
}

interface Stop {
  key: "sl" | "entry" | "t1" | "t2";
  label: string;
  value: number;
  tone: string;
  ariaTone: string;
}

function pct(value: number, min: number, max: number): number {
  if (max === min) return 50;
  return ((value - min) / (max - min)) * 100;
}

/**
 * Horizontal R:R visualisation. Renders the SL, entry, T1 (and optional T2) at proportional
 * positions with red/neutral/green zones between them. Width-only — caller controls outer container.
 */
export default function RiskRewardBar({
  stoploss,
  entry,
  target1,
  target2,
  direction = "long",
  className,
}: RiskRewardBarProps) {
  const stops: Stop[] = [
    { key: "sl", label: "SL", value: stoploss, tone: "bg-rec-danger", ariaTone: "stoploss" },
    { key: "entry", label: "Entry", value: entry, tone: "bg-rec-neutral", ariaTone: "entry" },
    { key: "t1", label: "T1", value: target1, tone: "bg-rec-success", ariaTone: "target one" },
  ];
  if (target2 !== undefined) {
    stops.push({
      key: "t2",
      label: "T2",
      value: target2,
      tone: "bg-rec-success",
      ariaTone: "target two",
    });
  }

  const values = stops.map((s) => s.value);
  const min = Math.min(...values);
  const max = Math.max(...values);

  // For shorts, lower is better — flip risk/reward zones.
  const riskColor = "bg-rec-danger/20";
  const rewardColor = "bg-rec-success/20";
  const slPos = pct(stoploss, min, max);
  const entryPos = pct(entry, min, max);
  const t1Pos = pct(target1, min, max);

  // Risk zone: between stoploss and entry. Reward zone: entry → last target.
  const riskLeft = direction === "long" ? slPos : entryPos;
  const riskWidth = Math.abs(entryPos - slPos);
  const lastTargetPos = target2 !== undefined ? pct(target2, min, max) : t1Pos;
  const rewardLeft = direction === "long" ? entryPos : lastTargetPos;
  const rewardWidth = Math.abs(lastTargetPos - entryPos);

  const ariaLabel = `Risk reward, stoploss ${formatINRPrecise(stoploss)}, entry ${formatINRPrecise(
    entry
  )}, target ${formatINRPrecise(target1)}${
    target2 !== undefined ? `, second target ${formatINRPrecise(target2)}` : ""
  }`;

  return (
    <div className={["w-full", className ?? ""].join(" ")} role="group" aria-label={ariaLabel}>
      <div className="relative h-2 rounded-full bg-rec-track">
        <div
          className={["absolute top-0 h-2 rounded-l-full", riskColor].join(" ")}
          style={{ left: `${riskLeft}%`, width: `${riskWidth}%` }}
          aria-hidden="true"
        />
        <div
          className={["absolute top-0 h-2 rounded-r-full", rewardColor].join(" ")}
          style={{ left: `${rewardLeft}%`, width: `${rewardWidth}%` }}
          aria-hidden="true"
        />
        {stops.map((s) => {
          const left = pct(s.value, min, max);
          return (
            <span
              key={s.key}
              className={[
                "absolute -top-1 w-1 h-4 rounded-sm ring-1 ring-rec-bg",
                s.tone,
              ].join(" ")}
              style={{ left: `calc(${left}% - 2px)` }}
              aria-label={`${s.ariaTone} at ${formatINRPrecise(s.value)}`}
              title={`${s.label}: ${formatINRPrecise(s.value)}`}
            />
          );
        })}
      </div>
      <div className="mt-2 flex justify-between text-[10px] tabular-nums text-rec-fg-muted">
        {stops.map((s) => (
          <span key={s.key} className="flex flex-col items-center">
            <span className="uppercase tracking-wide opacity-70">{s.label}</span>
            <span className="text-rec-fg">{formatINRPrecise(s.value)}</span>
          </span>
        ))}
      </div>
    </div>
  );
}
