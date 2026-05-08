// ApiKeyForm
// ---------------------------------------------------------------------------
// Strict invariant: this component NEVER renders a stored API key value back
// into the DOM. The placeholder shows "Configured ✓ (NN chars)" when a key is
// already saved server-side; the input is empty by default and only contains
// what the user is currently typing to overwrite the key. This invariant is
// covered by ApiKeyForm.test.tsx.

import { useEffect, useState, useId, useCallback } from "react";
import { Button, Field, TextInput } from "./_primitives";
import { savedKeyHint } from "./_utils";

export type ApiKeyProvider = "openai" | "gemini" | "claude";

export interface ApiKeyStatus {
  configured: boolean;
  charCount?: number | null;
}

export interface ApiKeyFormValue {
  provider: ApiKeyProvider;
  // The newly-entered key (only). Empty string ⇒ "do not change".
  newKey: string;
}

interface Props {
  // Map of provider → status. Comes from `GET /api/settings`. NEVER the key.
  statuses: Record<ApiKeyProvider, ApiKeyStatus>;
  // Persists `{ <provider>_api_key: newKey }`. Only called with non-empty keys.
  onSave: (value: ApiKeyFormValue) => Promise<void>;
  // Triggers a backend probe — caller decides how to display the result.
  onTest: (provider: ApiKeyProvider) => Promise<{ ok: boolean; message: string }>;
}

const PROVIDER_LABELS: Record<ApiKeyProvider, string> = {
  openai: "OpenAI",
  gemini: "Gemini",
  claude: "Claude (Anthropic)",
};

const PROVIDERS: ApiKeyProvider[] = ["openai", "gemini", "claude"];

export default function ApiKeyForm({ statuses, onSave, onTest }: Props) {
  return (
    <div className="flex flex-col gap-4">
      <p className="text-xs text-slate-400">
        API keys are stored locally and never synced to the cloud. We display
        whether a key is configured — never the key itself.
      </p>
      {PROVIDERS.map((p) => (
        <ProviderRow
          key={p}
          provider={p}
          status={statuses[p]}
          onSave={onSave}
          onTest={onTest}
        />
      ))}
    </div>
  );
}

interface RowProps {
  provider: ApiKeyProvider;
  status: ApiKeyStatus;
  onSave: Props["onSave"];
  onTest: Props["onTest"];
}

function ProviderRow({ provider, status, onSave, onTest }: RowProps) {
  const inputId = useId();
  const [newKey, setNewKey] = useState("");
  const [reveal, setReveal] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; message: string } | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);

  // Hard guard: if statuses change, ensure we never accidentally populate the
  // input with anything besides what the user has typed.
  useEffect(() => {
    setError(null);
  }, [status]);

  const handleSave = useCallback(async () => {
    if (newKey.trim().length === 0) {
      setError("Enter a key to overwrite.");
      return;
    }
    setError(null);
    setSaving(true);
    try {
      await onSave({ provider, newKey: newKey.trim() });
      setNewKey("");
      setReveal(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save key.");
    } finally {
      setSaving(false);
    }
  }, [newKey, onSave, provider]);

  const handleTest = useCallback(async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await onTest(provider);
      setTestResult(result);
    } catch (e) {
      setTestResult({
        ok: false,
        message: e instanceof Error ? e.message : "Test failed.",
      });
    } finally {
      setTesting(false);
    }
  }, [onTest, provider]);

  const placeholder = status.configured
    ? `${savedKeyHint(status.charCount)} — type to overwrite`
    : "Paste API key";

  return (
    <div className="rounded-md border border-slate-800 bg-slate-900/60 p-3">
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm font-medium text-slate-100">
          {PROVIDER_LABELS[provider]}
        </span>
        <span
          className={[
            "text-xs px-1.5 py-0.5 rounded",
            status.configured
              ? "bg-emerald-900/50 text-emerald-300"
              : "bg-slate-800 text-slate-400",
          ].join(" ")}
          aria-label={
            status.configured ? "API key configured" : "API key not configured"
          }
        >
          {status.configured ? "Configured" : "Not configured"}
        </span>
      </div>

      <Field
        label={`${PROVIDER_LABELS[provider]} API key`}
        htmlFor={inputId}
        error={error}
        hint={
          !error && status.configured
            ? "Leave blank to keep existing key. Saving overwrites it."
            : undefined
        }
      >
        <div className="flex gap-2">
          <TextInput
            id={inputId}
            // INVARIANT: `value` is bound only to local component state; we never
            // push the saved key into it.
            value={newKey}
            onChange={(e) => setNewKey(e.target.value)}
            type={reveal ? "text" : "password"}
            placeholder={placeholder}
            autoComplete="off"
            spellCheck={false}
            // Hint to password managers: this is a secret entry, not credentials
            // for the website. Disable autofill.
            data-1p-ignore
            data-lpignore="true"
            invalid={Boolean(error)}
          />
          <Button
            variant="ghost"
            onClick={() => setReveal((r) => !r)}
            aria-label={reveal ? "Hide entered key" : "Show entered key"}
            aria-pressed={reveal}
            // Only meaningful while user is typing — disable when empty.
            disabled={newKey.length === 0}
          >
            {reveal ? "Hide" : "Show"}
          </Button>
        </div>
      </Field>

      <div className="mt-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Button
            variant="primary"
            onClick={handleSave}
            loading={saving}
            disabled={newKey.length === 0}
          >
            Save
          </Button>
          <Button
            variant="secondary"
            onClick={handleTest}
            loading={testing}
            disabled={!status.configured && newKey.length === 0}
          >
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
