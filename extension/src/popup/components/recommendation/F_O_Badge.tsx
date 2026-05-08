import type { FAndOSignal } from "./types";

export type { FAndOSignal };

export interface FAndOBadgeProps {
  signal: FAndOSignal;
  className?: string;
}

const META: Record<FAndOSignal, { label: string; tone: string; explainer: string }> = {
  LONG_BUILDUP: {
    label: "Long Buildup",
    tone: "bg-rec-success/15 text-rec-success border-rec-success/30",
    explainer: "Open interest rising with price — fresh longs being built.",
  },
  SHORT_BUILDUP: {
    label: "Short Buildup",
    tone: "bg-rec-danger/15 text-rec-danger border-rec-danger/30",
    explainer: "Open interest rising with price falling — fresh shorts being built.",
  },
  LONG_UNWINDING: {
    label: "Long Unwind",
    tone: "bg-rec-warn/15 text-rec-warn border-rec-warn/30",
    explainer: "Open interest falling with price — longs exiting positions.",
  },
  SHORT_COVERING: {
    label: "Short Covering",
    tone: "bg-rec-info/15 text-rec-info border-rec-info/30",
    explainer: "Open interest falling with price rising — shorts buying back.",
  },
};

/**
 * F&O positioning chip. Each variant has a distinct label + tooltip so the meaning isn't
 * tied to colour alone.
 */
export default function F_O_Badge({ signal, className }: FAndOBadgeProps) {
  const m = META[signal];
  return (
    <span
      className={[
        "inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border text-[10px] font-medium",
        m.tone,
        className ?? "",
      ].join(" ")}
      title={m.explainer}
      aria-label={`F and O: ${m.label}. ${m.explainer}`}
    >
      <span aria-hidden="true" className="text-[9px] opacity-70 font-bold">
        F&amp;O
      </span>
      <span>{m.label}</span>
    </span>
  );
}
