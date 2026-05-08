import type { Horizon as LibHorizon } from "@/lib/types";

/** Accepts both the canonical horizon and the legacy 'positional' alias. */
export type Horizon = LibHorizon | "positional";

export interface HorizonBadgeProps {
  horizon: Horizon;
  className?: string;
}

const META: Record<Horizon, { label: string; tone: string; icon: string; aria: string }> = {
  intraday: {
    label: "Intraday",
    tone: "bg-rec-horizon-intraday/15 text-rec-horizon-intraday border-rec-horizon-intraday/30",
    icon: "I",
    aria: "Intraday horizon",
  },
  swing: {
    label: "Swing",
    tone: "bg-rec-horizon-swing/15 text-rec-horizon-swing border-rec-horizon-swing/30",
    icon: "S",
    aria: "Swing horizon",
  },
  long: {
    label: "Positional",
    tone: "bg-rec-horizon-positional/15 text-rec-horizon-positional border-rec-horizon-positional/30",
    icon: "P",
    aria: "Positional horizon",
  },
  positional: {
    label: "Positional",
    tone: "bg-rec-horizon-positional/15 text-rec-horizon-positional border-rec-horizon-positional/30",
    icon: "P",
    aria: "Positional horizon",
  },
};

/** Inline badge for trade horizon. Uses both icon glyph and label so colour isn't the only signal. */
export default function HorizonBadge({ horizon, className }: HorizonBadgeProps) {
  const m = META[horizon];
  return (
    <span
      className={[
        "inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md border text-[10px] font-medium uppercase tracking-wide",
        m.tone,
        className ?? "",
      ].join(" ")}
      aria-label={m.aria}
      title={m.aria}
    >
      <span aria-hidden="true" className="font-bold opacity-80">
        {m.icon}
      </span>
      <span>{m.label}</span>
    </span>
  );
}
