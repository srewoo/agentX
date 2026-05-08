// AlertConditionBuilder
// ---------------------------------------------------------------------------
// Friendly UI for building one of six alert condition kinds. Pure presentation
// — caller owns state via `value`/`onChange` and validation via `error`.
//
// We deliberately keep the kind selector and the param input close together
// so the user always sees what's required for the chosen kind.

import { useId } from "react";
import {
  Field,
  TextInput,
} from "../settings/_primitives";
import type { AlertCondition, AlertConditionKind } from "./_types";

interface Props {
  value: AlertCondition;
  onChange: (next: AlertCondition) => void;
  // Optional: per-kind error from the parent form.
  error?: string | null;
}

const KINDS: Array<{
  value: AlertConditionKind;
  label: string;
  description: string;
}> = [
  {
    value: "price_above",
    label: "Price crosses above",
    description: "Fires when last traded price > threshold",
  },
  {
    value: "price_below",
    label: "Price crosses below",
    description: "Fires when last traded price < threshold",
  },
  {
    value: "pct_change_1d_above",
    label: "1-day % change above",
    description: "Fires when intraday change exceeds threshold",
  },
  {
    value: "recommendation_conviction_above",
    label: "Recommendation conviction above",
    description: "Fires when AI conviction (0–10) exceeds threshold",
  },
  {
    value: "volume_spike_above",
    label: "Volume spike above",
    description: "Fires when volume exceeds N× the 20-day average",
  },
  {
    value: "breakout_N_day_high",
    label: "Breakout above N-day high",
    description: "Fires when price closes above the highest high of N days",
  },
];

// Type-safe transition: switching kind picks a sensible default param.
function defaultFor(kind: AlertConditionKind): AlertCondition {
  switch (kind) {
    case "price_above":
      return { kind, threshold: 0 };
    case "price_below":
      return { kind, threshold: 0 };
    case "pct_change_1d_above":
      return { kind, pct: 5 };
    case "recommendation_conviction_above":
      return { kind, conviction: 7 };
    case "volume_spike_above":
      return { kind, multiple: 2 };
    case "breakout_N_day_high":
      return { kind, days: 20 };
  }
}

export default function AlertConditionBuilder({ value, onChange, error }: Props) {
  const kindId = useId();
  const paramId = useId();

  return (
    <div className="flex flex-col gap-3">
      <Field
        label="When"
        htmlFor={kindId}
        hint={KINDS.find((k) => k.value === value.kind)?.description}
      >
        <select
          id={kindId}
          value={value.kind}
          onChange={(e) =>
            onChange(defaultFor(e.target.value as AlertConditionKind))
          }
          className="w-full rounded-md border border-slate-700 bg-slate-900 px-2.5 py-1.5 text-sm text-slate-100 focus:outline-none focus:ring-2 focus:ring-emerald-400"
        >
          {KINDS.map((k) => (
            <option key={k.value} value={k.value}>
              {k.label}
            </option>
          ))}
        </select>
      </Field>

      <ParamInput
        id={paramId}
        condition={value}
        onChange={onChange}
        error={error ?? null}
      />
    </div>
  );
}

// ── Per-kind input ────────────────────────────────────────────────────────

function ParamInput({
  id,
  condition,
  onChange,
  error,
}: {
  id: string;
  condition: AlertCondition;
  onChange: (next: AlertCondition) => void;
  error: string | null;
}) {
  switch (condition.kind) {
    case "price_above":
    case "price_below":
      return (
        <Field
          label="Threshold price (₹)"
          htmlFor={id}
          error={error}
          hint={!error ? "Numeric, in INR." : undefined}
        >
          <TextInput
            id={id}
            type="number"
            inputMode="decimal"
            min={0}
            step="0.05"
            value={Number.isFinite(condition.threshold) ? condition.threshold : ""}
            onChange={(e) =>
              onChange({ ...condition, threshold: Number(e.target.value) })
            }
            invalid={Boolean(error)}
          />
        </Field>
      );
    case "pct_change_1d_above":
      return (
        <Field
          label="Threshold (%)"
          htmlFor={id}
          error={error}
          hint={!error ? "Whole or decimal, e.g. 5 means +5% intraday." : undefined}
        >
          <TextInput
            id={id}
            type="number"
            inputMode="decimal"
            step="0.1"
            value={condition.pct}
            onChange={(e) =>
              onChange({ ...condition, pct: Number(e.target.value) })
            }
            invalid={Boolean(error)}
          />
        </Field>
      );
    case "recommendation_conviction_above":
      return (
        <Field
          label="Min conviction (0–10)"
          htmlFor={id}
          error={error}
        >
          <TextInput
            id={id}
            type="number"
            inputMode="numeric"
            min={0}
            max={10}
            step="1"
            value={condition.conviction}
            onChange={(e) =>
              onChange({ ...condition, conviction: Number(e.target.value) })
            }
            invalid={Boolean(error)}
          />
        </Field>
      );
    case "volume_spike_above":
      return (
        <Field
          label="Volume multiple (×)"
          htmlFor={id}
          error={error}
          hint={!error ? "e.g. 2 means twice the 20-day average volume." : undefined}
        >
          <TextInput
            id={id}
            type="number"
            inputMode="decimal"
            min={1}
            step="0.1"
            value={condition.multiple}
            onChange={(e) =>
              onChange({ ...condition, multiple: Number(e.target.value) })
            }
            invalid={Boolean(error)}
          />
        </Field>
      );
    case "breakout_N_day_high":
      return (
        <Field
          label="Lookback window (days)"
          htmlFor={id}
          error={error}
          hint={!error ? "Common: 20, 52 (weeks → 252)." : undefined}
        >
          <TextInput
            id={id}
            type="number"
            inputMode="numeric"
            min={2}
            max={252}
            step="1"
            value={condition.days}
            onChange={(e) =>
              onChange({ ...condition, days: Number(e.target.value) })
            }
            invalid={Boolean(error)}
          />
        </Field>
      );
  }
}

// ── Live preview helpers (pure) ───────────────────────────────────────────

export function previewCondition(symbol: string, c: AlertCondition): string {
  const sym = symbol.trim() || "this stock";
  switch (c.kind) {
    case "price_above":
      return `Alert when ${sym} crosses above ₹${formatNum(c.threshold)} today.`;
    case "price_below":
      return `Alert when ${sym} drops below ₹${formatNum(c.threshold)} today.`;
    case "pct_change_1d_above":
      return `Alert when ${sym} moves more than ${formatNum(c.pct)}% in a single day.`;
    case "recommendation_conviction_above":
      return `Alert when ${sym}'s AI conviction score is at least ${c.conviction} / 10.`;
    case "volume_spike_above":
      return `Alert when ${sym} trades more than ${formatNum(c.multiple)}× its 20-day average volume.`;
    case "breakout_N_day_high":
      return `Alert when ${sym} closes above its ${c.days}-day high.`;
  }
}

// Validation: returns null when valid, else a user-facing message.
export function validateCondition(c: AlertCondition): string | null {
  switch (c.kind) {
    case "price_above":
    case "price_below":
      if (!Number.isFinite(c.threshold) || c.threshold <= 0)
        return "Threshold must be a positive number.";
      return null;
    case "pct_change_1d_above":
      if (!Number.isFinite(c.pct) || c.pct <= 0)
        return "Percent must be greater than zero.";
      return null;
    case "recommendation_conviction_above":
      if (!Number.isInteger(c.conviction) || c.conviction < 0 || c.conviction > 10)
        return "Conviction must be a whole number between 0 and 10.";
      return null;
    case "volume_spike_above":
      if (!Number.isFinite(c.multiple) || c.multiple < 1)
        return "Multiple must be at least 1.";
      return null;
    case "breakout_N_day_high":
      if (!Number.isInteger(c.days) || c.days < 2 || c.days > 252)
        return "Lookback days must be a whole number between 2 and 252.";
      return null;
  }
}

function formatNum(n: number): string {
  if (!Number.isFinite(n)) return "—";
  // Up to 2 decimals, no trailing zeros.
  return n.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}
