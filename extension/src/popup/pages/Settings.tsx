import { useState } from "react";
import { useSettings } from "../hooks/useSettings";
import { LLM_MODELS } from "../../shared/constants";
import type { AppSettings } from "../../shared/types";

export default function Settings() {
  const { settings, loading, saving, update } = useSettings();
  const [saved, setSaved] = useState(false);
  const [backendUrl, setBackendUrl] = useState("");

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    await update(settings as Partial<AppSettings>);
    if (backendUrl) {
      const current = await (await import("../../shared/storage")).getSettings();
      await (await import("../../shared/storage")).saveSettings({ ...current, backend_url: backendUrl } as Partial<AppSettings>);
    }
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  };

  const set = (key: keyof AppSettings, value: unknown) =>
    update({ [key]: value } as Partial<AppSettings>);

  const provider = (settings.llm_provider || "gemini") as keyof typeof LLM_MODELS;
  const models = LLM_MODELS[provider] || [];

  if (loading) {
    return <div className="flex items-center justify-center h-full text-zinc-500 text-sm">Loading settings...</div>;
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 overflow-y-auto px-3 py-3 space-y-4">

        {/* Alert Settings */}
        <section>
          <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-2">Alert Engine</h3>
          <div className="space-y-3">
            <div>
              <label className="text-xs text-zinc-400 block mb-1">Scan Interval</label>
              <select
                value={settings.alert_interval_minutes ?? "30"}
                onChange={(e) => set("alert_interval_minutes", e.target.value)}
                className="w-full bg-zinc-800 border border-border rounded px-2 py-1.5 text-sm text-zinc-100"
              >
                <option value="0">Manual only</option>
                <option value="15">Every 15 minutes</option>
                <option value="30">Every 30 minutes</option>
                <option value="60">Every 60 minutes</option>
              </select>
              {String(settings.alert_interval_minutes) !== "0" && (
                <p className="text-[10px] text-zinc-600 mt-1">Auto-scan runs during market hours only (9:15 AM – 3:30 PM IST, Mon–Fri)</p>
              )}
            </div>

            <div>
              <label className="text-xs text-zinc-400 block mb-1">Risk Mode</label>
              <div className="flex gap-1">
                {["conservative", "balanced", "aggressive"].map((mode) => (
                  <button
                    key={mode}
                    onClick={() => set("risk_mode", mode)}
                    className={`flex-1 py-1.5 text-xs rounded border capitalize
                      ${settings.risk_mode === mode
                        ? "bg-brand text-white border-brand"
                        : "border-border text-zinc-400 hover:text-zinc-200"}`}
                  >
                    {mode}
                  </button>
                ))}
              </div>
              <div className="text-[10px] text-zinc-600 mt-1">
                Conservative: strength ≥7 · Balanced: ≥5 · Aggressive: ≥3
              </div>
            </div>

            <div>
              <label className="text-xs text-zinc-400 block mb-1">Signal Types</label>
              <div className="flex flex-wrap gap-1">
                {["intraday", "swing", "long_term"].map((type) => {
                  const types = (settings.signal_types || []) as string[];
                  const active = types.includes(type);
                  return (
                    <button
                      key={type}
                      onClick={() => {
                        const updated = active ? types.filter((t) => t !== type) : [...types, type];
                        set("signal_types", updated);
                      }}
                      className={`px-2 py-0.5 text-xs rounded border capitalize
                        ${active ? "bg-brand/20 text-brand-light border-brand/40" : "border-border text-zinc-500"}`}
                    >
                      {type.replace("_", " ")}
                    </button>
                  );
                })}
              </div>
            </div>
          </div>
        </section>

        {/* LLM Settings */}
        <section>
          <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-2">AI / LLM</h3>
          <div className="space-y-3">
            <div>
              <label className="text-xs text-zinc-400 block mb-1">Provider</label>
              <select
                value={settings.llm_provider || "gemini"}
                onChange={(e) => {
                  const p = e.target.value as keyof typeof LLM_MODELS;
                  set("llm_provider", p);
                  set("llm_model", LLM_MODELS[p]?.[0] || "");
                }}
                className="w-full bg-zinc-800 border border-border rounded px-2 py-1.5 text-sm text-zinc-100"
              >
                <option value="gemini">Google Gemini</option>
                <option value="openai">OpenAI</option>
                <option value="claude">Anthropic Claude</option>
              </select>
            </div>

            <div>
              <label className="text-xs text-zinc-400 block mb-1">Model</label>
              <select
                value={settings.llm_model || models[0]}
                onChange={(e) => set("llm_model", e.target.value)}
                className="w-full bg-zinc-800 border border-border rounded px-2 py-1.5 text-sm text-zinc-100"
              >
                {models.map((m) => <option key={m} value={m}>{m}</option>)}
              </select>
            </div>

            {["gemini", "openai", "claude"].map((p) => {
              const keyField = `${p}_api_key` as keyof AppSettings;
              const label = { gemini: "Gemini API Key", openai: "OpenAI API Key", claude: "Claude API Key" }[p];
              return (
                <div key={p}>
                  <label className="text-xs text-zinc-400 block mb-1">{label}</label>
                  <input
                    type="password"
                    value={(settings[keyField] as string) || ""}
                    onChange={(e) => set(keyField, e.target.value)}
                    placeholder={`Enter your ${label}...`}
                    className="w-full bg-zinc-800 border border-border rounded px-2 py-1.5 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none focus:border-brand"
                  />
                </div>
              );
            })}
          </div>
        </section>

        {/* Backend URL */}
        <section>
          <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-2">Backend</h3>
          <div>
            <label className="text-xs text-zinc-400 block mb-1">Backend URL</label>
            <input
              type="text"
              value={backendUrl || "http://localhost:8020"}
              onChange={(e) => setBackendUrl(e.target.value)}
              className="w-full bg-zinc-800 border border-border rounded px-2 py-1.5 text-xs text-zinc-100 focus:outline-none focus:border-brand"
            />
          </div>
        </section>
      </div>

      {/* Save button */}
      <div className="px-3 py-2 border-t border-border">
        <button
          onClick={handleSave}
          disabled={saving}
          className="w-full bg-brand text-white rounded-lg py-2 text-sm font-medium hover:bg-brand/80 disabled:opacity-50"
        >
          {saving ? "Saving..." : saved ? "✓ Saved!" : "Save Settings"}
        </button>
      </div>
    </div>
  );
}
