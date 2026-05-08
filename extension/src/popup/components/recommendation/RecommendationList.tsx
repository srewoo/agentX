import { useEffect, useMemo, useRef, useState } from "react";
import type { Recommendation, Horizon as LibHorizon } from "@/lib/types";
import { apiClient } from "@/lib/api";
import SignalCard from "./SignalCard";
import type { RecommendationView } from "./types";

type HorizonFilter = LibHorizon | "all";

export interface RecommendationListProps {
  /** Override data — when provided the list will not fetch. */
  recommendations?: readonly RecommendationView[];
  /** Called when a card is opened. */
  onSelect?: (rec: RecommendationView) => void;
  /** Initial filter values. */
  initialHorizon?: HorizonFilter;
  initialSector?: string;
  /** Initial minimum conviction in 0..100 scale. */
  initialMinConviction?: number;
  className?: string;
}

const VIRTUALIZE_THRESHOLD = 50;
const ITEM_HEIGHT_ESTIMATE = 220; // px — dense card height including gap
const OVERSCAN = 4;
const SECTOR_ALL = "__all__";
const SECTOR_NONE = "__none__"; // bucket for null sectors

function uniqueSectors(recs: readonly Recommendation[]): string[] {
  const set = new Set<string>();
  for (const r of recs) set.add(r.sector ?? SECTOR_NONE);
  return Array.from(set).sort((a, b) => {
    if (a === SECTOR_NONE) return 1;
    if (b === SECTOR_NONE) return -1;
    return a.localeCompare(b);
  });
}

function sectorMatches(rec: Recommendation, filter: string): boolean {
  if (filter === SECTOR_ALL) return true;
  if (filter === SECTOR_NONE) return rec.sector == null;
  return rec.sector === filter;
}

/**
 * Filterable, responsive recommendation grid. Owns:
 *  - Loading / error / empty states
 *  - Filters: horizon, sector, min conviction (in 0..100)
 *  - Lightweight windowing when items > 50 (no extra deps)
 *
 * The minimum-conviction slider is in 0..100 (display units) but the backend
 * filter is sent as a 0..1 fraction.
 */
export default function RecommendationList({
  recommendations,
  onSelect,
  initialHorizon = "all",
  initialSector = SECTOR_ALL,
  initialMinConviction = 0,
  className,
}: RecommendationListProps) {
  const [data, setData] = useState<readonly RecommendationView[] | null>(recommendations ?? null);
  const [loading, setLoading] = useState<boolean>(!recommendations);
  const [error, setError] = useState<string | null>(null);

  const [horizon, setHorizon] = useState<HorizonFilter>(initialHorizon);
  const [sector, setSector] = useState<string>(initialSector);
  const [minConviction, setMinConviction] = useState<number>(initialMinConviction);
  const [reloadKey, setReloadKey] = useState(0);

  // Fetch when uncontrolled.
  useEffect(() => {
    if (recommendations !== undefined) {
      setData(recommendations);
      setLoading(false);
      return;
    }
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    apiClient
      .getRecommendations({}, ctrl.signal)
      .then((res) => {
        setData(res);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (ctrl.signal.aborted) return;
        setError(e instanceof Error ? e.message : "Could not load recommendations.");
        setLoading(false);
      });
    return () => ctrl.abort();
  }, [recommendations, reloadKey]);

  const sectors = useMemo(() => (data ? uniqueSectors(data) : []), [data]);

  const filtered = useMemo<readonly RecommendationView[]>(() => {
    if (!data) return [];
    // Normalize convPct once — conviction may be 0..1 or 0..100 from different backends.
    return data.filter((r) => {
      if (horizon !== "all" && r.horizon !== horizon) return false;
      if (!sectorMatches(r, sector)) return false;
      const convPct = r.conviction <= 1 ? r.conviction * 100 : r.conviction;
      if (convPct < minConviction) return false;
      return true;
    });
  }, [data, horizon, sector, minConviction]);

  return (
    <section
      className={["flex flex-col gap-3", className ?? ""].join(" ")}
      aria-label="Recommendations"
    >
      <Filters
        horizon={horizon}
        sector={sector}
        sectors={sectors}
        minConviction={minConviction}
        onHorizon={setHorizon}
        onSector={setSector}
        onMinConviction={setMinConviction}
        disabled={loading || Boolean(error)}
      />

      {loading && <SkeletonGrid count={4} />}
      {!loading && error && <ErrorState message={error} onRetry={() => setReloadKey((k) => k + 1)} />}
      {!loading && !error && filtered.length === 0 && <EmptyState minConviction={minConviction} />}
      {!loading && !error && filtered.length > 0 && <Grid items={filtered} onSelect={onSelect} />}
    </section>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Filters
// ─────────────────────────────────────────────────────────────────────────────

interface FiltersProps {
  horizon: HorizonFilter;
  sector: string;
  sectors: readonly string[];
  minConviction: number;
  onHorizon: (h: HorizonFilter) => void;
  onSector: (s: string) => void;
  onMinConviction: (n: number) => void;
  disabled?: boolean;
}

function Filters({
  horizon,
  sector,
  sectors,
  minConviction,
  onHorizon,
  onSector,
  onMinConviction,
  disabled,
}: FiltersProps) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-[11px]">
      <label className="flex items-center gap-1 text-rec-fg-muted">
        <span className="sr-only">Horizon</span>
        <select
          aria-label="Filter by horizon"
          disabled={disabled}
          value={horizon}
          onChange={(e) => onHorizon(e.target.value as HorizonFilter)}
          className="bg-rec-surface border border-rec-border rounded px-1.5 py-1 text-rec-fg disabled:opacity-50"
        >
          <option value="all">All horizons</option>
          <option value="intraday">Intraday</option>
          <option value="swing">Swing</option>
          <option value="long">Positional</option>
        </select>
      </label>

      <label className="flex items-center gap-1 text-rec-fg-muted">
        <span className="sr-only">Sector</span>
        <select
          aria-label="Filter by sector"
          disabled={disabled || sectors.length === 0}
          value={sector}
          onChange={(e) => onSector(e.target.value)}
          className="bg-rec-surface border border-rec-border rounded px-1.5 py-1 text-rec-fg disabled:opacity-50 max-w-[140px]"
        >
          <option value={SECTOR_ALL}>All sectors</option>
          {sectors.map((s) => (
            <option key={s} value={s}>
              {s === SECTOR_NONE ? "(no sector)" : s}
            </option>
          ))}
        </select>
      </label>

      <label className="flex items-center gap-2 text-rec-fg-muted ml-auto">
        <span>Min conviction</span>
        <input
          type="range"
          min={0}
          max={100}
          step={5}
          value={minConviction}
          onChange={(e) => onMinConviction(Number(e.target.value))}
          aria-label="Minimum conviction"
          aria-valuetext={`${minConviction}`}
          disabled={disabled}
          className="accent-rec-info disabled:opacity-50"
        />
        <span className="tabular-nums text-rec-fg w-6 text-right">{minConviction}</span>
      </label>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Grid (with optional windowing)
// ─────────────────────────────────────────────────────────────────────────────

interface GridProps {
  items: readonly RecommendationView[];
  onSelect?: (r: RecommendationView) => void;
}

function Grid({ items, onSelect }: GridProps) {
  if (items.length <= VIRTUALIZE_THRESHOLD) {
    return (
      <div
        className="grid gap-2 grid-cols-1 sm:grid-cols-2"
        role="list"
        aria-label={`${items.length} recommendations`}
      >
        {items.map((r) => (
          <div role="listitem" key={r.id}>
            <SignalCard recommendation={r} onSelect={onSelect} />
          </div>
        ))}
      </div>
    );
  }
  return <WindowedGrid items={items} onSelect={onSelect} />;
}

function WindowedGrid({ items, onSelect }: GridProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewport, setViewport] = useState(480);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const onScroll = () => setScrollTop(el.scrollTop);
    const onResize = () => setViewport(el.clientHeight);
    setViewport(el.clientHeight);
    el.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onResize);
    return () => {
      el.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onResize);
    };
  }, []);

  const total = items.length;
  const start = Math.max(0, Math.floor(scrollTop / ITEM_HEIGHT_ESTIMATE) - OVERSCAN);
  const end = Math.min(total, Math.ceil((scrollTop + viewport) / ITEM_HEIGHT_ESTIMATE) + OVERSCAN);
  const visible = items.slice(start, end);
  const offsetY = start * ITEM_HEIGHT_ESTIMATE;
  const totalHeight = total * ITEM_HEIGHT_ESTIMATE;

  return (
    <div
      ref={scrollRef}
      className="overflow-y-auto max-h-[480px] pr-1"
      role="list"
      aria-label={`${total} recommendations`}
    >
      <div style={{ height: totalHeight, position: "relative" }}>
        <div style={{ transform: `translateY(${offsetY}px)` }} className="flex flex-col gap-2">
          {visible.map((r) => (
            <div role="listitem" key={r.id}>
              <SignalCard recommendation={r} onSelect={onSelect} />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// States
// ─────────────────────────────────────────────────────────────────────────────

function SkeletonGrid({ count }: { count: number }) {
  return (
    <div className="grid gap-2 grid-cols-1 sm:grid-cols-2" aria-busy="true" aria-live="polite">
      {Array.from({ length: count }).map((_, i) => (
        <div
          key={i}
          className="h-[180px] rounded-xl border border-rec-border bg-rec-surface animate-pulse"
          aria-hidden="true"
        />
      ))}
      <span className="sr-only">Loading recommendations…</span>
    </div>
  );
}

function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div
      role="alert"
      className="rounded-lg border border-rec-danger/40 bg-rec-danger/10 p-3 text-[12px] text-rec-danger flex items-center justify-between gap-2"
    >
      <span>Couldn’t load recommendations: {message}</span>
      <button
        type="button"
        onClick={onRetry}
        className="px-2 py-1 text-[11px] rounded border border-rec-danger/40 hover:bg-rec-danger/20 focus-visible:ring-2 focus-visible:ring-rec-focus"
      >
        Retry
      </button>
    </div>
  );
}

function EmptyState({ minConviction }: { minConviction: number }) {
  return (
    <div
      role="status"
      className="rounded-lg border border-rec-border bg-rec-surface p-4 text-center text-[12px] text-rec-fg-muted"
    >
      <p className="text-rec-fg font-medium mb-1">No high-conviction signals right now</p>
      <p>
        {minConviction > 0
          ? "Try lowering the conviction floor or relaxing your filters."
          : "Check back after the next scan — markets are quiet."}
      </p>
    </div>
  );
}
