import { useState, useId, type KeyboardEvent } from "react";
import { formatINR, formatINRPrecise, formatPct } from "@/lib/format";
import {
  type RecommendationView,
  type Action,
  actionFor,
  convictionPct,
  deriveRiskReward,
} from "./types";
import ConvictionGauge from "./ConvictionGauge";
import HorizonBadge from "./HorizonBadge";
import RiskRewardBar from "./RiskRewardBar";
import ReasonsList from "./ReasonsList";
import FactorRadar from "./FactorRadar";
import FlowsBadge from "./FlowsBadge";
import F_O_Badge from "./F_O_Badge";

export interface SignalCardProps {
  recommendation: RecommendationView;
  /** Fires when the card is activated (click / keyboard). */
  onSelect?: (rec: RecommendationView) => void;
  className?: string;
}

// Tiny presentational button used for the in-card "Watch" / "Alert" quick
// actions. Defined inline rather than spun out — these are the only two
// callers and the styling is bespoke to this card.
function ActionButton({
  icon,
  label,
  ariaLabel,
  onClick,
}: {
  icon: string;
  label: string;
  ariaLabel: string;
  onClick: (e: React.MouseEvent) => void | Promise<void>;
}) {
  return (
    <button
      type="button"
      aria-label={ariaLabel}
      onClick={(e) => {
        void onClick(e);
      }}
      className="text-[11px] px-2 py-0.5 rounded border border-rec-border text-rec-fg-muted hover:text-rec-fg hover:border-rec-border-strong active:scale-[0.98] transition"
    >
      <span aria-hidden="true" className="mr-1">{icon}</span>
      {label}
    </button>
  );
}

const ACTION_TONE: Record<Action, { label: string; cls: string; aria: string }> = {
  BUY: { label: "BUY", cls: "text-rec-success bg-rec-success/15 border-rec-success/30", aria: "Buy signal" },
  SELL: { label: "SELL", cls: "text-rec-danger bg-rec-danger/15 border-rec-danger/30", aria: "Sell signal" },
  HOLD: { label: "HOLD", cls: "text-rec-warn bg-rec-warn/15 border-rec-warn/30", aria: "Hold signal" },
  AVOID: { label: "AVOID", cls: "text-rec-fg-muted bg-rec-neutral/15 border-rec-neutral/30", aria: "Avoid signal" },
};

const CAP_BAND_LABEL: Record<NonNullable<RecommendationView["marketCapBand"]>, string> = {
  LARGE: "Large",
  MID: "Mid",
  SMALL: "Small",
  MICRO: "Micro",
};

function isLong(action: Action): "long" | "short" {
  return action === "SELL" ? "short" : "long";
}

/**
 * Dense, clickable recommendation card. Shows action, conviction, entry → target,
 * stoploss, R:R, top reasons. Hover (on pointer-fine devices) reveals factor radar.
 *
 * Accessibility:
 *  - Card is a real interactive element (role=button + tabIndex) so Enter / Space
 *    activate it natively. When `onSelect` is omitted the card becomes a passive group.
 *  - aria-label summarises symbol + action + conviction so screen readers get the gist.
 *  - Each chip exposes its meaning via title + aria-label so colour is never the only signal.
 */
export default function SignalCard({ recommendation: r, onSelect, className }: SignalCardProps) {
  const [hovered, setHovered] = useState(false);
  const headingId = useId();

  const action = ACTION_TONE[actionFor(r)];
  const convPct = convictionPct(r);
  const rr = deriveRiskReward(r);

  const dayPct = r.priceChangePct1d;
  const dirArrow =
    typeof dayPct !== "number" ? "" : dayPct > 0 ? "▲" : dayPct < 0 ? "▼" : "▬";
  const dayTone =
    typeof dayPct !== "number"
      ? "text-rec-fg-muted"
      : dayPct > 0
        ? "text-rec-success"
        : dayPct < 0
          ? "text-rec-danger"
          : "text-rec-fg-muted";

  const stop = r.stopLoss ?? null;
  const entry = r.entryPrice ?? null;
  const t1 = r.target ?? null;
  const t2 = typeof r.target2 === "number" ? r.target2 : undefined;
  const showRRBar = stop !== null && entry !== null && t1 !== null;

  const ariaParts: string[] = [
    `${r.symbol} on ${r.exchange}`,
    `${action.label}`,
    `conviction ${convPct} of 100`,
  ];
  if (entry !== null) ariaParts.push(`entry ${formatINRPrecise(entry)}`);
  if (t1 !== null) ariaParts.push(`target ${formatINRPrecise(t1)}`);
  if (stop !== null) ariaParts.push(`stoploss ${formatINRPrecise(stop)}`);
  if (rr > 0) ariaParts.push(`risk reward ${rr.toFixed(2)} to 1`);
  const ariaLabel = ariaParts.join(", ");

  const interactive = Boolean(onSelect);

  function handleKey(e: KeyboardEvent<HTMLDivElement>) {
    if (!onSelect) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onSelect(r);
    }
  }

  const lastPrice = r.lastPrice ?? r.entryPrice ?? null;
  const sectorLabel = r.sector ?? "Sector unknown";

  return (
    <div
      className={[
        "group relative rounded-xl border border-rec-border bg-rec-surface p-3 shadow-sm",
        "transition-all duration-150 outline-none",
        interactive
          ? "cursor-pointer hover:border-rec-border-strong focus-visible:ring-2 focus-visible:ring-rec-focus"
          : "",
        className ?? "",
      ].join(" ")}
      role={interactive ? "button" : "group"}
      tabIndex={interactive ? 0 : -1}
      aria-labelledby={headingId}
      aria-label={ariaLabel}
      onClick={interactive ? () => onSelect!(r) : undefined}
      onKeyDown={interactive ? handleKey : undefined}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onFocus={() => setHovered(true)}
      onBlur={() => setHovered(false)}
      data-testid="rec-signal-card"
    >
      {/* Top row: symbol + chips */}
      <div className="flex items-center gap-1.5 flex-wrap">
        <h3 id={headingId} className="text-sm font-semibold text-rec-fg leading-none">
          {r.symbol}
        </h3>
        <span
          className="text-[10px] font-medium px-1 py-0.5 rounded border border-rec-border text-rec-fg-muted"
          aria-label={`Listed on ${r.exchange}`}
          title={r.exchange}
        >
          {r.exchange}
        </span>
        {r.marketCapBand && (
          <span
            className="text-[10px] px-1 py-0.5 rounded border border-rec-border text-rec-fg-muted"
            aria-label={`${CAP_BAND_LABEL[r.marketCapBand]} cap`}
            title={`${CAP_BAND_LABEL[r.marketCapBand]} cap`}
          >
            {CAP_BAND_LABEL[r.marketCapBand]}
          </span>
        )}
        <HorizonBadge horizon={r.horizon} />
        {lastPrice !== null && (
          <span className="ml-auto flex items-center gap-1.5 text-[11px] tabular-nums">
            <span className="text-rec-fg">{formatINR(lastPrice)}</span>
            {typeof dayPct === "number" && (
              <span className={dayTone} aria-label={`Day change ${formatPct(dayPct)}`}>
                <span aria-hidden="true">{dirArrow}</span> {formatPct(dayPct)}
              </span>
            )}
          </span>
        )}
      </div>

      {/* Big row: action verb + conviction */}
      <div className="mt-2 flex items-center justify-between gap-3">
        <div className="flex items-center gap-1.5 flex-wrap">
          <span
            className={[
              "inline-flex items-center px-2 py-1 rounded-md border text-sm font-bold tracking-wide",
              action.cls,
            ].join(" ")}
            aria-label={`${action.aria}; research signal, not investment advice`}
          >
            {action.label}
          </span>
          <span
            className="text-[10px] px-1.5 py-0.5 rounded border border-rec-border bg-rec-bg/70 text-rec-fg-muted"
            title="Generated by analytics rules. Not personalised investment advice."
          >
            Research signal
          </span>
        </div>

        <div className="flex items-center gap-2">
          {r.fiiDiiSignal && <FlowsBadge signal={r.fiiDiiSignal} />}
          {r.fAndOSignal && <F_O_Badge signal={r.fAndOSignal} />}
          {typeof r.deliveryPct === "number" && (
            <span
              className="text-[10px] px-1.5 py-0.5 rounded-md border border-rec-border text-rec-fg-muted"
              title={`Delivery ${r.deliveryPct.toFixed(1)}% of traded volume`}
              aria-label={`Delivery ${r.deliveryPct.toFixed(1)} percent`}
            >
              {r.deliveryPct.toFixed(0)}% Del
            </span>
          )}
          <ConvictionGauge value={convPct} size="md" />
        </div>
      </div>

      {/* Middle row: entry → target, stoploss, R:R */}
      <div className="mt-3">
        <div className="flex items-center justify-between text-[11px] text-rec-fg-muted gap-2">
          <span className="min-w-0 truncate">
            {entry !== null ? (
              <>
                Entry <span className="text-rec-fg tabular-nums">{formatINRPrecise(entry)}</span>
                {t1 !== null && (
                  <>
                    <span aria-hidden="true" className="mx-1">
                      →
                    </span>
                    <span className="text-rec-fg tabular-nums">{formatINRPrecise(t1)}</span>
                  </>
                )}
                {t2 !== undefined && (
                  <span className="ml-1 text-rec-fg-muted tabular-nums">/ {formatINRPrecise(t2)}</span>
                )}
              </>
            ) : (
              <span className="italic">Levels pending</span>
            )}
          </span>
          <span className="flex items-center gap-2 shrink-0">
            {stop !== null && (
              <span
                className="px-1.5 py-0.5 rounded border border-rec-danger/40 text-rec-danger text-[10px] tabular-nums"
                aria-label={`Stoploss ${formatINRPrecise(stop)}`}
                title={`Stoploss ${formatINRPrecise(stop)}`}
              >
                SL {formatINRPrecise(stop)}
              </span>
            )}
            {rr > 0 && (
              <span
                className="px-1.5 py-0.5 rounded border border-rec-border bg-rec-bg/60 text-rec-fg text-[10px] tabular-nums"
                aria-label={`Risk reward ${rr.toFixed(2)} to 1`}
                title={`Risk : Reward = 1 : ${rr.toFixed(2)}`}
              >
                R:R 1:{rr.toFixed(2)}
              </span>
            )}
          </span>
        </div>
        {showRRBar && (
          <div className="mt-2">
            <RiskRewardBar
              stoploss={stop}
              entry={entry}
              target1={t1}
              target2={t2}
              direction={isLong(actionFor(r))}
            />
          </div>
        )}
      </div>

      {/* Sector + reasons */}
      <div className="mt-3 flex items-start gap-2">
        <span
          className="text-[10px] px-1.5 py-0.5 rounded-full border border-rec-border text-rec-fg-muted shrink-0"
          aria-label={`Sector ${sectorLabel}`}
        >
          {sectorLabel}
        </span>
        <div className="min-w-0 flex-1">
          <ReasonsList reasons={r.rationale} limit={3} />
        </div>
      </div>

      {(r.regime || r.dataQuality || typeof r.factorAgreement === "number") && (
        <div className="mt-2 flex flex-wrap gap-1.5 text-[10px] text-rec-fg-muted">
          {r.regime && (
            <span className="px-1.5 py-0.5 rounded border border-rec-border">
              Regime {String(r.regime).replace("_", " ")}
            </span>
          )}
          {r.dataQuality && (
            <span className="px-1.5 py-0.5 rounded border border-rec-border">
              Data {String(r.dataQuality).replace("_", " ")}
            </span>
          )}
          {typeof r.factorAgreement === "number" && (
            <span className="px-1.5 py-0.5 rounded border border-rec-border">
              Agreement {Math.round(r.factorAgreement * 100)}%
            </span>
          )}
        </div>
      )}

      <p className="mt-2 text-[10px] leading-snug text-rec-fg-muted">
        {r.advisoryDisclaimer ??
          "Research signal only, not investment advice. Validate independently and use your own risk controls."}
      </p>
      {r.calibrationNote && (
        <p className="mt-1 text-[10px] leading-snug text-rec-fg-muted">
          {r.calibrationNote}
        </p>
      )}
      {r.portfolioContext?.notes && r.portfolioContext.notes.length > 0 && (
        <p className="mt-1 text-[10px] leading-snug text-rec-fg-muted">
          Portfolio: {r.portfolioContext.notes[0]}
        </p>
      )}

      {/* Quick actions row — `stopPropagation` so the button click doesn't
       *  trigger the card's onSelect navigation. */}
      <div className="mt-3 flex items-center gap-1.5 pt-2 border-t border-rec-border">
        <ActionButton
          icon="★"
          label="Watch"
          ariaLabel={`Add ${r.symbol} to watchlist`}
          onClick={async (e) => {
            e.stopPropagation();
            try {
              const { api } = await import("../../../shared/api");
              await api.addToWatchlist(r.symbol, r.symbol);
            } catch {
              // Silently no-op — user-facing toast is owned by the page.
            }
          }}
        />
        <ActionButton
          icon="🔔"
          label="Alert"
          ariaLabel={`Set price alert for ${r.symbol}`}
          onClick={async (e) => {
            e.stopPropagation();
            try {
              const { api } = await import("../../../shared/api");
              const tgt = entry ?? lastPrice;
              if (tgt && tgt > 0) {
                await api.createAlert(
                  r.symbol,
                  tgt,
                  actionFor(r) === "SELL" ? "below" : "above",
                  `Auto from ${r.horizon} signal`,
                );
              }
            } catch {
              /* swallow; toasting is owned at page-level */
            }
          }}
        />
      </div>

      {/* Hover-reveal factor radar (pointer-fine only). Hidden from a11y tree to avoid noise — full radar lives on the detail screen. */}
      {hovered && r.signals && r.signals.length > 0 && (
        <div
          className="hidden md:block absolute -right-2 -top-2 translate-x-full bg-rec-surface border border-rec-border rounded-lg p-2 shadow-lg z-10 pointer-events-none"
          aria-hidden="true"
        >
          <FactorRadar signals={r.signals} size={120} />
        </div>
      )}

      <span className="sr-only">Generated at {new Date(r.generatedAt).toLocaleString()}</span>
    </div>
  );
}
