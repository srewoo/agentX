// ThemeToggle — light / dark / system
// ---------------------------------------------------------------------------
// Implemented as a radio group, since the three options are mutually exclusive.

import { useId } from "react";

export type ThemePreference = "light" | "dark" | "system";

interface Props {
  value: ThemePreference;
  onChange: (next: ThemePreference) => void;
}

const OPTIONS: Array<{ value: ThemePreference; label: string; hint: string }> = [
  { value: "light", label: "Light", hint: "Bright UI" },
  { value: "dark", label: "Dark", hint: "Dim UI" },
  { value: "system", label: "System", hint: "Follow OS" },
];

export default function ThemeToggle({ value, onChange }: Props) {
  const groupId = useId();
  return (
    <div className="rounded-md border border-slate-800 bg-slate-900/60 p-3">
      <h3 id={groupId} className="text-sm font-medium text-slate-100 mb-2">
        Theme
      </h3>
      <div role="radiogroup" aria-labelledby={groupId} className="flex gap-2">
        {OPTIONS.map((opt) => {
          const selected = value === opt.value;
          return (
            <button
              key={opt.value}
              role="radio"
              type="button"
              aria-checked={selected}
              onClick={() => onChange(opt.value)}
              className={[
                "flex-1 rounded-md border px-2 py-1.5 text-xs text-left",
                "focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400",
                selected
                  ? "border-emerald-500 bg-emerald-900/40 text-emerald-100"
                  : "border-slate-700 bg-slate-900 text-slate-300 hover:bg-slate-800",
              ].join(" ")}
            >
              <div className="font-medium">{opt.label}</div>
              <div className="text-[11px] opacity-70">{opt.hint}</div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
