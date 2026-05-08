// Tiny, accessibility-first primitives shared across settings + alerts.
// Kept local (no dep on a design system) but consistent in shape.

import {
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  forwardRef,
  useEffect,
  useRef,
  useId,
} from "react";

// ── Button ────────────────────────────────────────────────────────────────
type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  loading?: boolean;
}

const VARIANT_CLASS: Record<ButtonVariant, string> = {
  primary:
    "bg-emerald-600 hover:bg-emerald-500 text-white disabled:bg-emerald-900 disabled:text-emerald-300",
  secondary:
    "bg-slate-700 hover:bg-slate-600 text-slate-100 disabled:bg-slate-800 disabled:text-slate-500",
  ghost:
    "bg-transparent hover:bg-slate-800 text-slate-300 disabled:text-slate-600",
  danger:
    "bg-rose-600 hover:bg-rose-500 text-white disabled:bg-rose-900 disabled:text-rose-300",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  function Button(
    { variant = "secondary", loading, className = "", children, disabled, ...rest },
    ref,
  ) {
    return (
      <button
        ref={ref}
        type={rest.type ?? "button"}
        aria-busy={loading || undefined}
        disabled={disabled || loading}
        className={[
          "inline-flex items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium",
          "transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400",
          "disabled:cursor-not-allowed",
          VARIANT_CLASS[variant],
          className,
        ].join(" ")}
        {...rest}
      >
        {loading ? (
          <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-current border-t-transparent" />
        ) : null}
        {children}
      </button>
    );
  },
);

// ── Field (label + error wrapper) ─────────────────────────────────────────
interface FieldProps {
  label: string;
  htmlFor: string;
  hint?: ReactNode;
  error?: string | null;
  children: ReactNode;
}

export function Field({ label, htmlFor, hint, error, children }: FieldProps) {
  const errId = `${htmlFor}-err`;
  const hintId = `${htmlFor}-hint`;
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={htmlFor} className="text-xs font-medium text-slate-300">
        {label}
      </label>
      {children}
      {hint && !error ? (
        <p id={hintId} className="text-xs text-slate-500">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={errId} role="alert" className="text-xs text-rose-400">
          {error}
        </p>
      ) : null}
    </div>
  );
}

// ── TextInput ─────────────────────────────────────────────────────────────
interface TextInputProps extends InputHTMLAttributes<HTMLInputElement> {
  invalid?: boolean;
}

export const TextInput = forwardRef<HTMLInputElement, TextInputProps>(
  function TextInput({ invalid, className = "", ...rest }, ref) {
    return (
      <input
        ref={ref}
        aria-invalid={invalid || undefined}
        className={[
          "w-full rounded-md border bg-slate-900 px-2.5 py-1.5 text-sm text-slate-100",
          "placeholder:text-slate-500",
          "focus:outline-none focus:ring-2",
          invalid
            ? "border-rose-500 focus:ring-rose-400"
            : "border-slate-700 focus:ring-emerald-400 focus:border-emerald-500",
          className,
        ].join(" ")}
        {...rest}
      />
    );
  },
);

// ── Switch (accessible toggle) ────────────────────────────────────────────
interface SwitchProps {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  id?: string;
  disabled?: boolean;
}

export function Switch({ checked, onChange, label, id, disabled }: SwitchProps) {
  const generated = useId();
  const elId = id ?? `sw-${generated}`;
  return (
    <button
      id={elId}
      role="switch"
      type="button"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={[
        "relative inline-flex h-5 w-9 items-center rounded-full transition-colors",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400",
        checked ? "bg-emerald-600" : "bg-slate-700",
        disabled ? "opacity-50 cursor-not-allowed" : "cursor-pointer",
      ].join(" ")}
    >
      <span
        className={[
          "inline-block h-4 w-4 transform rounded-full bg-white transition-transform",
          checked ? "translate-x-4" : "translate-x-0.5",
        ].join(" ")}
      />
    </button>
  );
}

// ── Modal / Dialog (accessible: focus trap, ESC, return focus) ────────────
interface DialogProps {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  children: ReactNode;
  // Footer rendered in the modal — keep buttons outside `<form>` if needed.
  footer?: ReactNode;
  // For form dialogs — when set, content is wrapped in <form onSubmit>.
  onSubmit?: (e: React.FormEvent<HTMLFormElement>) => void;
  // Optional max-width override.
  size?: "sm" | "md" | "lg";
}

export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  onSubmit,
  size = "md",
}: DialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);
  const titleId = useId();
  const descId = useId();

  // Focus trap + return focus
  useEffect(() => {
    if (!open) return;
    previouslyFocused.current = document.activeElement as HTMLElement | null;

    // Focus first focusable element inside dialog
    const dialog = dialogRef.current;
    if (dialog) {
      const focusable = dialog.querySelector<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      );
      focusable?.focus();
    }

    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key === "Tab" && dialog) {
        const focusables = Array.from(
          dialog.querySelectorAll<HTMLElement>(
            'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
          ),
        ).filter((el) => !el.hasAttribute("aria-hidden"));
        if (focusables.length === 0) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      // Return focus to trigger
      previouslyFocused.current?.focus?.();
    };
  }, [open, onClose]);

  if (!open) return null;

  const widthClass =
    size === "sm" ? "max-w-sm" : size === "lg" ? "max-w-xl" : "max-w-md";

  const body = (
    <>
      <div className="px-4 py-3 border-b border-slate-800">
        <h2 id={titleId} className="text-sm font-semibold text-slate-100">
          {title}
        </h2>
        {description ? (
          <p id={descId} className="mt-1 text-xs text-slate-400">
            {description}
          </p>
        ) : null}
      </div>
      <div className="px-4 py-3 max-h-[60vh] overflow-y-auto">{children}</div>
      {footer ? (
        <div className="px-4 py-3 border-t border-slate-800 flex justify-end gap-2">
          {footer}
        </div>
      ) : null}
    </>
  );

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        className={`w-full ${widthClass} rounded-lg bg-slate-900 shadow-2xl border border-slate-800`}
      >
        {onSubmit ? (
          <form onSubmit={onSubmit} noValidate>
            {body}
          </form>
        ) : (
          body
        )}
      </div>
    </div>
  );
}

// ── Toast (lightweight, single message at a time) ─────────────────────────
interface ToastState {
  kind: "success" | "error" | "info";
  message: string;
}

export function Toast({
  toast,
  onDismiss,
}: {
  toast: ToastState | null;
  onDismiss: () => void;
}) {
  useEffect(() => {
    if (!toast) return;
    const id = setTimeout(onDismiss, 4000);
    return () => clearTimeout(id);
  }, [toast, onDismiss]);

  if (!toast) return null;
  const tone =
    toast.kind === "success"
      ? "bg-emerald-700 text-emerald-50"
      : toast.kind === "error"
        ? "bg-rose-700 text-rose-50"
        : "bg-slate-700 text-slate-50";
  return (
    <div
      role="status"
      aria-live="polite"
      className={`fixed bottom-3 left-1/2 -translate-x-1/2 z-[60] rounded-md px-3 py-1.5 text-sm shadow-lg ${tone}`}
    >
      {toast.message}
    </div>
  );
}

export type { ToastState };
