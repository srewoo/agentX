// BackendUrlForm
// ---------------------------------------------------------------------------
// Edits the backend URL the extension talks to. Validates protocol/host shape
// and offers a "Test connection" probe that the parent wires to api.health().

import { useId, useState, useEffect } from "react";
import { Button, Field, TextInput } from "./_primitives";
import { isValidUrl } from "./_utils";

interface Props {
  value: string;
  onSave: (next: string) => Promise<void>;
  onTest: (url: string) => Promise<{ ok: boolean; message: string }>;
}

export default function BackendUrlForm({ value, onSave, onTest }: Props) {
  const id = useId();
  const [draft, setDraft] = useState(value);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(
    null,
  );

  useEffect(() => {
    setDraft(value);
  }, [value]);

  const trimmed = draft.trim();
  const dirty = trimmed !== value;

  const handleSave = async () => {
    if (!isValidUrl(trimmed)) {
      setError("Enter a full URL (http:// or https://).");
      return;
    }
    setError(null);
    setSaving(true);
    try {
      // Strip trailing slash for consistency.
      await onSave(trimmed.replace(/\/$/, ""));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save.");
    } finally {
      setSaving(false);
    }
  };

  const handleTest = async () => {
    if (!isValidUrl(trimmed)) {
      setError("Enter a full URL (http:// or https://).");
      return;
    }
    setError(null);
    setTesting(true);
    setTestResult(null);
    try {
      setTestResult(await onTest(trimmed.replace(/\/$/, "")));
    } catch (e) {
      setTestResult({
        ok: false,
        message: e instanceof Error ? e.message : "Connection failed.",
      });
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="rounded-md border border-slate-800 bg-slate-900/60 p-3 flex flex-col gap-3">
      <h3 className="text-sm font-medium text-slate-100">Backend URL</h3>
      <Field
        label="API base URL"
        htmlFor={id}
        error={error}
        hint={
          !error
            ? "Default: http://localhost:8000. Change if you self-host."
            : undefined
        }
      >
        <TextInput
          id={id}
          type="url"
          inputMode="url"
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value);
            if (error) setError(null);
          }}
          placeholder="https://agentx.example.com"
          invalid={Boolean(error)}
          autoComplete="off"
          spellCheck={false}
        />
      </Field>
      <div className="flex items-center justify-between">
        <div className="flex gap-2">
          <Button
            variant="primary"
            onClick={handleSave}
            loading={saving}
            disabled={!dirty}
          >
            Save
          </Button>
          <Button variant="secondary" onClick={handleTest} loading={testing}>
            Test connection
          </Button>
        </div>
        {testResult ? (
          <span
            role="status"
            className={`text-xs ${
              testResult.ok ? "text-emerald-400" : "text-rose-400"
            }`}
          >
            {testResult.ok ? "✓ " : "✗ "}
            {testResult.message}
          </span>
        ) : null}
      </div>
    </div>
  );
}
