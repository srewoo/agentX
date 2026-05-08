// SpendCapForm
// ---------------------------------------------------------------------------
// LLM daily spend cap with progress bar.
// • Currency toggle USD / INR (cosmetic display — we always store the cap in USD
//   on the backend; INR view uses the supplied fxRate to convert).
// • Visual states:
//     <80%   → emerald
//     80-99% → amber
//     >=100% → rose + "blocking" announcement
// • Progress bar uses role="progressbar" with proper aria-* values.

import { useEffect, useState, useId } from "react";
import { Button, Field, TextInput } from "./_primitives";
import { type Currency, clamp, formatMoney } from "./_utils";

export interface LlmUsageToday {
  spentUsd: number;
  capUsd: number;
}

interface Props {
  usage: LlmUsageToday;
  // 1 USD = `fxRate` INR; provided by parent (cached).
  fxRate: number;
  onSave: (capUsd: number) => Promise<void>;
}

export default function SpendCapForm({ usage, fxRate, onSave }: Props) {
  const id = useId();
  const [currency, setCurrency] = useState<Currency>("USD");
  const [capDraft, setCapDraft] = useState<string>(usage.capUsd.toFixed(2));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);

  // Sync draft when remote cap changes (e.g. cross-device update).
  useEffect(() => {
    setCapDraft(
      currency === "USD"
        ? usage.capUsd.toFixed(2)
        : (usage.capUsd * fxRate).toFixed(0),
    );
  }, [usage.capUsd, currency, fxRate]);

  const pct = usage.capUsd > 0
    ? clamp((usage.spentUsd / usage.capUsd) * 100, 0, 999)
    : 0;
  const tone =
    pct >= 100
      ? "bg-rose-500"
      : pct >= 80
        ? "bg-amber-500"
        : "bg-emerald-500";
  const blocking = pct >= 100;

  const handleSave = async () => {
    const parsed = Number(capDraft);
    if (!Number.isFinite(parsed) || parsed <= 0) {
      setError("Enter a positive number.");
      return;
    }
    const capUsd = currency === "INR" ? parsed / fxRate : parsed;
    setError(null);
    setSaving(true);
    try {
      await onSave(Number(capUsd.toFixed(4)));
      setSavedAt(Date.now());
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save cap.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rounded-md border border-slate-800 bg-slate-900/60 p-3 flex flex-col gap-3">
      <header className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-slate-100">LLM Daily Spend Cap</h3>
        <div
          role="radiogroup"
          aria-label="Display currency"
          className="inline-flex rounded-md border border-slate-700 overflow-hidden text-xs"
        >
          {(["USD", "INR"] as Currency[]).map((c) => (
            <button
              key={c}
              role="radio"
              aria-checked={currency === c}
              type="button"
              onClick={() => setCurrency(c)}
              className={[
                "px-2 py-1",
                currency === c
                  ? "bg-emerald-600 text-white"
                  : "bg-slate-900 text-slate-300 hover:bg-slate-800",
              ].join(" ")}
            >
              {c}
            </button>
          ))}
        </div>
      </header>

      <UsageBar
        spentUsd={usage.spentUsd}
        capUsd={usage.capUsd}
        currency={currency}
        fxRate={fxRate}
        tone={tone}
        pct={pct}
        blocking={blocking}
      />

      <Field
        label={`Daily cap (${currency})`}
        htmlFor={id}
        error={error}
        hint={
          !error && currency === "INR"
            ? `Stored as USD; using fx 1 USD = ₹${fxRate.toFixed(2)}`
            : undefined
        }
      >
        <div className="flex gap-2 items-center">
          <span
            aria-hidden="true"
            className="px-2 py-1.5 rounded-md bg-slate-800 text-slate-300 text-sm"
          >
            {currency === "USD" ? "$" : "₹"}
          </span>
          <TextInput
            id={id}
            type="number"
            inputMode="decimal"
            min={0}
            step={currency === "USD" ? "0.01" : "1"}
            value={capDraft}
            onChange={(e) => setCapDraft(e.target.value)}
            invalid={Boolean(error)}
          />
        </div>
      </Field>

      <div className="flex items-center justify-between">
        <Button variant="primary" onClick={handleSave} loading={saving}>
          Save cap
        </Button>
        {savedAt ? (
          <span role="status" className="text-xs text-emerald-400">
            ✓ Saved
          </span>
        ) : null}
      </div>
    </div>
  );
}

function UsageBar({
  spentUsd,
  capUsd,
  currency,
  fxRate,
  tone,
  pct,
  blocking,
}: {
  spentUsd: number;
  capUsd: number;
  currency: Currency;
  fxRate: number;
  tone: string;
  pct: number;
  blocking: boolean;
}) {
  const display = (usd: number) =>
    formatMoney(currency === "INR" ? usd * fxRate : usd, currency);

  return (
    <div>
      <div className="flex items-baseline justify-between text-xs text-slate-300">
        <span>
          Spent today: <strong>{display(spentUsd)}</strong>
        </span>
        <span>of {display(capUsd)}</span>
      </div>
      <div
        role="progressbar"
        aria-valuenow={Math.round(pct)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="LLM daily spend"
        className="mt-1 h-2 w-full rounded-full bg-slate-800 overflow-hidden"
      >
        <div
          className={`h-full ${tone} transition-[width] duration-300`}
          style={{ width: `${Math.min(pct, 100)}%` }}
        />
      </div>
      {blocking ? (
        <p role="alert" className="mt-1 text-xs text-rose-400">
          Daily cap reached. New LLM calls are blocked until tomorrow or until
          the cap is raised.
        </p>
      ) : pct >= 80 ? (
        <p className="mt-1 text-xs text-amber-400">
          Approaching your daily cap ({Math.round(pct)}% used).
        </p>
      ) : null}
    </div>
  );
}
