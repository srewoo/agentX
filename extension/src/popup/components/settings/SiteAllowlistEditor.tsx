// SiteAllowlistEditor
// ---------------------------------------------------------------------------
// Manages the host patterns the content script may inject into.
// Supports bare hosts (`example.com`), wildcard subdomains (`*.example.com`),
// and host with optional `/*` path glob — same shape used by the manifest's
// runtime injection design.

import { useId, useState } from "react";
import { Button, Field, TextInput } from "./_primitives";
import { isValidHostPattern } from "./_utils";

interface Props {
  patterns: string[];
  onChange: (next: string[]) => Promise<void>;
}

export default function SiteAllowlistEditor({ patterns, onChange }: Props) {
  const id = useId();
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  const add = async () => {
    const v = draft.trim().toLowerCase();
    if (!v) return;
    if (!isValidHostPattern(v)) {
      setError(
        "Invalid pattern. Use a domain like `example.com` or `*.example.com`.",
      );
      return;
    }
    if (patterns.includes(v)) {
      setError("This pattern is already in the list.");
      return;
    }
    setError(null);
    await onChange([...patterns, v]);
    setDraft("");
  };

  const remove = async (p: string) => {
    await onChange(patterns.filter((x) => x !== p));
  };

  return (
    <div className="rounded-md border border-slate-800 bg-slate-900/60 p-3 flex flex-col gap-3">
      <h3 className="text-sm font-medium text-slate-100">Allowed sites</h3>
      <p className="text-xs text-slate-400">
        The content overlay only injects on these hosts. We never inject on
        sites you haven't approved.
      </p>

      <Field label="Add a host pattern" htmlFor={id} error={error}>
        <div className="flex gap-2">
          <TextInput
            id={id}
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value);
              if (error) setError(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void add();
              }
            }}
            placeholder="*.moneycontrol.com"
            invalid={Boolean(error)}
            autoComplete="off"
            spellCheck={false}
          />
          <Button variant="primary" onClick={() => void add()} disabled={!draft.trim()}>
            Add
          </Button>
        </div>
      </Field>

      {patterns.length === 0 ? (
        <p className="text-xs text-slate-500">No sites approved yet.</p>
      ) : (
        <ul aria-label="Allowed sites" className="flex flex-col gap-1">
          {patterns.map((p) => (
            <li
              key={p}
              className="flex items-center justify-between rounded border border-slate-800 px-2 py-1 text-sm bg-slate-900"
            >
              <span className="font-mono text-slate-200">{p}</span>
              <button
                type="button"
                onClick={() => void remove(p)}
                aria-label={`Remove ${p}`}
                className="text-xs text-slate-400 hover:text-rose-400 focus:outline-none focus:ring-1 focus:ring-rose-400 rounded px-1"
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
