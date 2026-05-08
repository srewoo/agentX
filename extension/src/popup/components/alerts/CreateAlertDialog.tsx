// CreateAlertDialog
// ---------------------------------------------------------------------------
// Modal for creating a new alert. Wraps:
//   • symbol input (free-text — assumes parent has search elsewhere)
//   • AlertConditionBuilder
//   • channel multi-select (only enabled channels are selectable)
//   • optional note
//   • live preview ("This will alert when RELIANCE crosses ₹2,900 within today")
//
// Validation runs on submit. The dialog uses the accessible `Dialog` primitive
// (focus trap, escape, return focus, aria-modal).

import { useEffect, useId, useState } from "react";
import { Dialog, Field, TextInput, Button } from "../settings/_primitives";
import AlertConditionBuilder, {
  previewCondition,
  validateCondition,
} from "./AlertConditionBuilder";
import type {
  Alert,
  AlertChannel,
  AlertCondition,
  AlertDraft,
} from "./_types";

interface Props {
  open: boolean;
  onClose: () => void;
  onCreate: (draft: AlertDraft) => Promise<Alert>;
  // Channels currently enabled in user settings — others are disabled in the UI.
  enabledChannels: AlertChannel[];
}

const CHANNEL_LABELS: Record<AlertChannel, string> = {
  telegram: "Telegram",
  email: "Email",
  whatsapp: "WhatsApp",
  sms: "SMS",
};

const ALL_CHANNELS: AlertChannel[] = ["telegram", "email", "whatsapp", "sms"];

const INITIAL_CONDITION: AlertCondition = {
  kind: "price_above",
  threshold: 0,
};

export default function CreateAlertDialog({
  open,
  onClose,
  onCreate,
  enabledChannels,
}: Props) {
  const symbolId = useId();
  const noteId = useId();

  const [symbol, setSymbol] = useState("");
  const [condition, setCondition] = useState<AlertCondition>(INITIAL_CONDITION);
  const [channels, setChannels] = useState<AlertChannel[]>([]);
  const [note, setNote] = useState("");

  const [errors, setErrors] = useState<{
    symbol?: string;
    condition?: string;
    channels?: string;
  }>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // Reset on re-open.
  useEffect(() => {
    if (open) {
      setSymbol("");
      setCondition(INITIAL_CONDITION);
      setChannels(enabledChannels.slice(0, 1));
      setNote("");
      setErrors({});
      setSubmitError(null);
    }
  }, [open, enabledChannels]);

  const validate = (): boolean => {
    const next: typeof errors = {};
    const s = symbol.trim().toUpperCase();
    if (!s) next.symbol = "Symbol is required.";
    else if (!/^[A-Z0-9.\-&]{1,16}$/.test(s))
      next.symbol = "Use letters, digits, '.', '-', '&' (max 16 chars).";

    const condErr = validateCondition(condition);
    if (condErr) next.condition = condErr;

    if (channels.length === 0)
      next.channels = "Pick at least one notification channel.";

    setErrors(next);
    return Object.keys(next).length === 0;
  };

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!validate()) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      await onCreate({
        symbol: symbol.trim().toUpperCase(),
        condition,
        channels,
        note: note.trim() || undefined,
      });
      onClose();
    } catch (err) {
      setSubmitError(
        err instanceof Error ? err.message : "Failed to create alert.",
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Create alert"
      description="We'll notify you on the channels you choose when this condition is met."
      size="lg"
      onSubmit={handleSubmit}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <Button variant="primary" type="submit" loading={submitting}>
            Create alert
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <Field
          label="Symbol (NSE)"
          htmlFor={symbolId}
          error={errors.symbol}
          hint={!errors.symbol ? "e.g. RELIANCE, TCS, HDFCBANK" : undefined}
        >
          <TextInput
            id={symbolId}
            value={symbol}
            onChange={(e) => setSymbol(e.target.value.toUpperCase())}
            placeholder="RELIANCE"
            autoComplete="off"
            spellCheck={false}
            invalid={Boolean(errors.symbol)}
          />
        </Field>

        <AlertConditionBuilder
          value={condition}
          onChange={setCondition}
          error={errors.condition ?? null}
        />

        <ChannelPicker
          selected={channels}
          onChange={setChannels}
          enabled={enabledChannels}
          error={errors.channels ?? null}
        />

        <Field
          label="Note (optional)"
          htmlFor={noteId}
          hint="Shown on the alert and in notifications."
        >
          <TextInput
            id={noteId}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="e.g. swing entry zone"
            maxLength={120}
          />
        </Field>

        <div
          aria-live="polite"
          className="rounded-md border border-emerald-900/60 bg-emerald-950/30 px-2.5 py-1.5 text-xs text-emerald-200"
        >
          {previewCondition(symbol, condition)}
        </div>

        {submitError ? (
          <p role="alert" className="text-xs text-rose-400">
            {submitError}
          </p>
        ) : null}
      </div>
    </Dialog>
  );
}

function ChannelPicker({
  selected,
  onChange,
  enabled,
  error,
}: {
  selected: AlertChannel[];
  onChange: (next: AlertChannel[]) => void;
  enabled: AlertChannel[];
  error: string | null;
}) {
  const groupId = useId();
  const enabledSet = new Set(enabled);

  const toggle = (ch: AlertChannel) => {
    if (selected.includes(ch)) onChange(selected.filter((c) => c !== ch));
    else onChange([...selected, ch]);
  };

  return (
    <fieldset className="flex flex-col gap-1.5">
      <legend id={groupId} className="text-xs font-medium text-slate-300">
        Notify via
      </legend>
      <div role="group" aria-labelledby={groupId} className="flex flex-wrap gap-2">
        {ALL_CHANNELS.map((ch) => {
          const isEnabled = enabledSet.has(ch);
          const isSelected = selected.includes(ch);
          return (
            <label
              key={ch}
              className={[
                "inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs cursor-pointer",
                "focus-within:ring-2 focus-within:ring-emerald-400",
                !isEnabled
                  ? "border-slate-800 bg-slate-900 text-slate-600 cursor-not-allowed"
                  : isSelected
                    ? "border-emerald-500 bg-emerald-900/40 text-emerald-100"
                    : "border-slate-700 bg-slate-900 text-slate-200 hover:bg-slate-800",
              ].join(" ")}
            >
              <input
                type="checkbox"
                className="sr-only"
                disabled={!isEnabled}
                checked={isSelected}
                onChange={() => toggle(ch)}
              />
              <span aria-hidden="true">{isSelected ? "✓" : ""}</span>
              <span>{CHANNEL_LABELS[ch]}</span>
              {!isEnabled ? (
                <span className="text-[10px] text-slate-500">(disabled)</span>
              ) : null}
            </label>
          );
        })}
      </div>
      {error ? (
        <p role="alert" className="text-xs text-rose-400">
          {error}
        </p>
      ) : (
        <p className="text-xs text-slate-500">
          Configure channels in Settings → Channels to enable more options.
        </p>
      )}
    </fieldset>
  );
}
