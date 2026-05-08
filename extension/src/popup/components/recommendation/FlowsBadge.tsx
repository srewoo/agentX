import type { FiiDiiSignal } from "./types";

export type { FiiDiiSignal };

export interface FlowsBadgeProps {
  signal: FiiDiiSignal;
  /** Optional override for the label (e.g. "FII", "DII", "FII/DII") */
  label?: string;
  className?: string;
}

const META: Record<FiiDiiSignal, { tone: string; arrow: string; verb: string }> = {
  INFLOW: { tone: "bg-rec-success/15 text-rec-success border-rec-success/30", arrow: "▲", verb: "inflow" },
  OUTFLOW: { tone: "bg-rec-danger/15 text-rec-danger border-rec-danger/30", arrow: "▼", verb: "outflow" },
  NEUTRAL: { tone: "bg-rec-neutral/15 text-rec-fg-muted border-rec-neutral/30", arrow: "▬", verb: "flat" },
};

/**
 * Tiny chip indicating institutional flows. Shape (arrow) + colour together so colour-blind users
 * can still distinguish inflow vs outflow.
 */
export default function FlowsBadge({ signal, label = "FII/DII", className }: FlowsBadgeProps) {
  const m = META[signal];
  const tooltip = `${label} ${m.verb}`;
  return (
    <span
      className={[
        "inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border text-[10px] font-medium",
        m.tone,
        className ?? "",
      ].join(" ")}
      title={tooltip}
      aria-label={tooltip}
    >
      <span aria-hidden="true">{m.arrow}</span>
      <span>{label}</span>
    </span>
  );
}
