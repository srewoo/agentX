/**
 * Indian-market formatters.
 * INR uses Indian numbering (lakh / crore) — never the western K/M/B scale.
 */

const RUPEE = "₹";

/** Compact INR: 12,34,567 → "₹12.35 L", 1,23,45,678 → "₹1.23 Cr". */
export function formatINR(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const sign = value < 0 ? "-" : "";
  const abs = Math.abs(value);

  if (abs >= 1_00_00_000) {
    return `${sign}${RUPEE}${(abs / 1_00_00_000).toFixed(2)} Cr`;
  }
  if (abs >= 1_00_000) {
    return `${sign}${RUPEE}${(abs / 1_00_000).toFixed(2)} L`;
  }
  if (abs >= 1_000) {
    return `${sign}${RUPEE}${(abs / 1_000).toFixed(2)} K`;
  }
  return `${sign}${RUPEE}${abs.toFixed(2)}`;
}

/** Precise INR with Indian grouping: 1234567.89 → "₹12,34,567.89". */
export function formatINRPrecise(
  value: number | null | undefined,
  fractionDigits = 2,
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const sign = value < 0 ? "-" : "";
  const abs = Math.abs(value);

  // Intl en-IN handles Indian grouping correctly across modern engines.
  const formatted = new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  }).format(abs);

  return `${sign}${RUPEE}${formatted}`;
}

/**
 * Percent. Accepts either a fraction (0.0234) or a whole percent (2.34).
 * Heuristic: |x| <= 1 → fraction; else already a percent.
 */
export function formatPct(
  value: number | null | undefined,
  opts: { fractionDigits?: number; alwaysSign?: boolean } = {},
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const { fractionDigits = 2, alwaysSign = true } = opts;
  const pct = Math.abs(value) <= 1 ? value * 100 : value;
  const sign = pct > 0 && alwaysSign ? "+" : pct < 0 ? "-" : alwaysSign ? "+" : "";
  return `${sign}${Math.abs(pct).toFixed(fractionDigits)}%`;
}

/** Tailwind class for change coloring. */
export function pctColorClass(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "text-zinc-400";
  }
  if (value > 0) return "text-profit";
  if (value < 0) return "text-loss";
  return "text-zinc-400";
}

/** Volume in lakh/crore short form: 1234567 → "12.35L". */
export function formatVolume(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const abs = Math.abs(value);
  const sign = value < 0 ? "-" : "";
  if (abs >= 1_00_00_000) return `${sign}${(abs / 1_00_00_000).toFixed(2)}Cr`;
  if (abs >= 1_00_000) return `${sign}${(abs / 1_00_000).toFixed(2)}L`;
  if (abs >= 1_000) return `${sign}${(abs / 1_000).toFixed(2)}K`;
  return `${sign}${abs.toFixed(0)}`;
}

/** "+2.34" / "-1.20" — used for raw price changes. */
export function formatChange(value: number | null | undefined, fractionDigits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const sign = value > 0 ? "+" : value < 0 ? "-" : "";
  return `${sign}${Math.abs(value).toFixed(fractionDigits)}`;
}
