import { useEffect, useId, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { TransactionSide, NewTransactionInput } from "@/lib/types";

export interface AddTransactionDialogProps {
  open: boolean;
  onClose: () => void;
  /** Called after a successful POST. */
  onSuccess?: (created: NewTransactionInput) => void;
  /** Optional toast hook. Falls back to inline status if not provided. */
  toast?: (msg: { type: "success" | "error"; text: string }) => void;
}

interface FormState {
  symbol: string;
  exchange: string;
  side: TransactionSide;
  qty: string;
  price: string;
  fees: string;
  notes: string;
}

const INITIAL: FormState = {
  symbol: "",
  exchange: "NSE",
  side: "buy",
  qty: "",
  price: "",
  fees: "",
  notes: "",
};

type Errors = Partial<Record<keyof FormState, string>>;

function validate(form: FormState): Errors {
  const errors: Errors = {};
  if (!form.symbol.trim()) errors.symbol = "Symbol is required";
  else if (!/^[A-Z0-9.\-&]{1,20}$/.test(form.symbol.trim().toUpperCase()))
    errors.symbol = "Use letters, digits, dot or dash";

  if (!form.exchange.trim()) errors.exchange = "Exchange is required";

  const qty = Number(form.qty);
  if (!form.qty.trim()) errors.qty = "Quantity is required";
  else if (!Number.isFinite(qty) || qty <= 0) errors.qty = "Quantity must be > 0";
  else if (!Number.isInteger(qty)) errors.qty = "Whole number only";

  const price = Number(form.price);
  if (!form.price.trim()) errors.price = "Price is required";
  else if (!Number.isFinite(price) || price <= 0) errors.price = "Price must be > 0";

  if (form.fees.trim()) {
    const fees = Number(form.fees);
    if (!Number.isFinite(fees) || fees < 0) errors.fees = "Fees must be ≥ 0";
  }

  if (form.notes.length > 280) errors.notes = "Keep notes under 280 characters";

  return errors;
}

export default function AddTransactionDialog({ open, onClose, onSuccess, toast }: AddTransactionDialogProps) {
  const [form, setForm] = useState<FormState>(INITIAL);
  const [errors, setErrors] = useState<Errors>({});
  const [touched, setTouched] = useState<Partial<Record<keyof FormState, boolean>>>({});
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const titleId = useId();
  const firstFieldRef = useRef<HTMLInputElement | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);

  // Reset on open / close, focus first field, restore body scroll lock.
  useEffect(() => {
    if (!open) return;
    setForm(INITIAL);
    setErrors({});
    setTouched({});
    setSubmitError(null);
    setSubmitting(false);
    const t = setTimeout(() => firstFieldRef.current?.focus(), 0);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      clearTimeout(t);
      document.body.style.overflow = prevOverflow;
    };
  }, [open]);

  // Esc to close.
  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // Live validation when fields are touched.
  useEffect(() => {
    setErrors(validate(form));
  }, [form]);

  if (!open) return null;

  const isValid = Object.keys(errors).length === 0;

  function update<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }

  function markTouched(key: keyof FormState) {
    setTouched((t) => ({ ...t, [key]: true }));
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setTouched({ symbol: true, exchange: true, side: true, qty: true, price: true, fees: true, notes: true });
    const errs = validate(form);
    setErrors(errs);
    if (Object.keys(errs).length > 0) return;

    const ex = form.exchange.trim().toUpperCase();
    const payload: NewTransactionInput = {
      symbol: form.symbol.trim().toUpperCase(),
      exchange: (ex === "NSE" || ex === "BSE" ? ex : "NSE") as "NSE" | "BSE",
      side: form.side,
      qty: Number(form.qty),
      price: Number(form.price),
      fees: form.fees.trim() ? Number(form.fees) : undefined,
      note: form.notes.trim() || undefined,
    };

    setSubmitting(true);
    setSubmitError(null);
    try {
      await api.portfolio.createTransaction(payload);
      toast?.({ type: "success", text: `Recorded ${payload.side.toUpperCase()} ${payload.qty} ${payload.symbol}` });
      onSuccess?.(payload);
      onClose();
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to save transaction";
      setSubmitError(msg);
      toast?.({ type: "error", text: msg });
    } finally {
      setSubmitting(false);
    }
  }

  function showError(key: keyof FormState): string | undefined {
    return touched[key] ? errors[key] : undefined;
  }

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={(e) => {
        if (e.target === dialogRef.current) onClose();
      }}
    >
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-md rounded-xl border border-neutral-800 bg-neutral-950 p-4 shadow-xl"
        noValidate
      >
        <div className="flex items-start justify-between">
          <h2 id={titleId} className="text-base font-semibold text-neutral-100">
            Add transaction
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="rounded p-1 text-neutral-400 hover:bg-neutral-800 hover:text-neutral-200"
          >
            ×
          </button>
        </div>

        <div className="mt-3 grid grid-cols-2 gap-3">
          <Field label="Symbol" error={showError("symbol")}>
            <input
              ref={firstFieldRef}
              type="text"
              value={form.symbol}
              onChange={(e) => update("symbol", e.target.value.toUpperCase())}
              onBlur={() => markTouched("symbol")}
              autoComplete="off"
              className="input"
              aria-invalid={Boolean(showError("symbol"))}
              required
            />
          </Field>

          <Field label="Exchange" error={showError("exchange")}>
            <select
              value={form.exchange}
              onChange={(e) => update("exchange", e.target.value)}
              onBlur={() => markTouched("exchange")}
              className="input"
            >
              <option value="NSE">NSE</option>
              <option value="BSE">BSE</option>
            </select>
          </Field>

          <Field label="Side" error={showError("side")}>
            <div className="flex gap-2" role="radiogroup" aria-label="Side">
              {(["buy", "sell"] as TransactionSide[]).map((s) => (
                <label
                  key={s}
                  className={`flex-1 cursor-pointer rounded-md border px-3 py-1.5 text-center text-sm font-medium ${
                    form.side === s
                      ? s === "buy"
                        ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300"
                        : "border-rose-500/40 bg-rose-500/15 text-rose-300"
                      : "border-neutral-700 text-neutral-400 hover:text-neutral-200"
                  }`}
                >
                  <input
                    type="radio"
                    name="side"
                    value={s}
                    checked={form.side === s}
                    onChange={() => update("side", s)}
                    className="sr-only"
                  />
                  {s.toUpperCase()}
                </label>
              ))}
            </div>
          </Field>

          <Field label="Quantity" error={showError("qty")}>
            <input
              type="number"
              inputMode="numeric"
              min={1}
              step={1}
              value={form.qty}
              onChange={(e) => update("qty", e.target.value)}
              onBlur={() => markTouched("qty")}
              className="input"
              aria-invalid={Boolean(showError("qty"))}
              required
            />
          </Field>

          <Field label="Price (₹)" error={showError("price")}>
            <input
              type="number"
              inputMode="decimal"
              min={0}
              step="0.01"
              value={form.price}
              onChange={(e) => update("price", e.target.value)}
              onBlur={() => markTouched("price")}
              className="input"
              aria-invalid={Boolean(showError("price"))}
              required
            />
          </Field>

          <Field label="Fees (₹, optional)" error={showError("fees")}>
            <input
              type="number"
              inputMode="decimal"
              min={0}
              step="0.01"
              value={form.fees}
              onChange={(e) => update("fees", e.target.value)}
              onBlur={() => markTouched("fees")}
              className="input"
              aria-invalid={Boolean(showError("fees"))}
            />
          </Field>

          <div className="col-span-2">
            <Field label={`Notes (${form.notes.length}/280)`} error={showError("notes")}>
              <textarea
                value={form.notes}
                onChange={(e) => update("notes", e.target.value)}
                onBlur={() => markTouched("notes")}
                rows={2}
                maxLength={280}
                className="input resize-none"
              />
            </Field>
          </div>
        </div>

        {submitError ? (
          <div role="alert" className="mt-3 rounded-md border border-red-500/30 bg-red-500/10 p-2 text-xs text-red-300">
            {submitError}
          </div>
        ) : null}

        <div className="mt-4 flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="rounded-md px-3 py-1.5 text-sm text-neutral-300 hover:bg-neutral-800"
          >
            Cancel
          </button>
          <button
            type="submit"
            disabled={!isValid || submitting}
            className="rounded-md bg-emerald-500/20 px-3 py-1.5 text-sm font-medium text-emerald-300 hover:bg-emerald-500/30 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting ? "Saving…" : "Save transaction"}
          </button>
        </div>

        <style>{`
          .input {
            width: 100%;
            background: rgb(23 23 23);
            border: 1px solid rgb(64 64 64);
            border-radius: 0.375rem;
            padding: 0.375rem 0.5rem;
            font-size: 0.875rem;
            color: rgb(229 229 229);
          }
          .input:focus {
            outline: none;
            border-color: rgb(16 185 129 / 0.6);
            box-shadow: 0 0 0 2px rgb(16 185 129 / 0.2);
          }
          .input[aria-invalid="true"] {
            border-color: rgb(244 63 94 / 0.6);
          }
        `}</style>
      </form>
    </div>
  );
}

function Field({ label, error, children }: { label: string; error?: string; children: React.ReactNode }) {
  return (
    <label className="block text-xs">
      <span className="mb-1 block font-medium text-neutral-400">{label}</span>
      {children}
      {error ? (
        <span role="alert" className="mt-1 block text-[11px] text-rose-400">
          {error}
        </span>
      ) : null}
    </label>
  );
}

export { validate as __validateForTest };
