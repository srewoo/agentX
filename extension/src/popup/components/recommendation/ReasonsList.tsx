export interface ReasonsListProps {
  reasons: readonly string[];
  /** Truncate to this many items (default 3). Set to 0 / undefined to show all. */
  limit?: number;
  /** Compact, single-column dense rendering (default true). */
  dense?: boolean;
  className?: string;
}

/**
 * Renders the bullet rationale behind a recommendation. Designed for tight popup widths —
 * each item gets a leading marker so reasons remain scannable when text wraps.
 */
export default function ReasonsList({ reasons, limit = 3, dense = true, className }: ReasonsListProps) {
  if (!reasons || reasons.length === 0) {
    return (
      <p className={["text-[11px] text-rec-fg-muted italic", className ?? ""].join(" ")}>
        No rationale provided.
      </p>
    );
  }
  const items = limit && limit > 0 ? reasons.slice(0, limit) : reasons;
  const remainder = reasons.length - items.length;

  return (
    <ul
      className={[
        "list-none m-0 p-0",
        dense ? "space-y-1 text-[11px]" : "space-y-1.5 text-xs",
        className ?? "",
      ].join(" ")}
      aria-label="Reasons"
    >
      {items.map((r, i) => (
        <li key={`${i}-${r.slice(0, 24)}`} className="flex gap-1.5 text-rec-fg leading-snug">
          <span aria-hidden="true" className="text-rec-fg-muted shrink-0 mt-[2px]">
            •
          </span>
          <span className="min-w-0 break-words">{r}</span>
        </li>
      ))}
      {remainder > 0 && (
        <li className="text-[10px] text-rec-fg-muted pl-3">+ {remainder} more</li>
      )}
    </ul>
  );
}
