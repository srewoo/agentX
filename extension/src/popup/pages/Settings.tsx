import { useState } from "react";
import { useSettings } from "../hooks/useSettings";
import { LLM_MODELS } from "../../shared/constants";
import type { AppSettings } from "../../shared/types";
import { downloadFile } from "../../shared/localStore";
import { saveSettings, getSettings } from "../../shared/storage";
import { api } from "../../shared/api";
import BrokerPanel from "../components/BrokerPanel";

// Sensitive keys to scrub from JSON exports
const SENSITIVE_EXPORT = new Set([
  "llm_api_key", "openai_api_key", "gemini_api_key", "claude_api_key", "api_key",
  "telegram_bot_token",
  // Broker credentials must never appear in user-exported config dumps.
  "angelone_api_key", "angelone_client_code", "angelone_mpin", "angelone_totp_secret",
  "kite_api_key", "kite_api_secret", "kite_access_token",
]);

export default function Settings() {
  const { settings, loading, saving, update } = useSettings();
  const [saved, setSaved] = useState(false);
  const [backendUrl, setBackendUrl] = useState("");
  const [importMsg, setImportMsg] = useState<string | null>(null);

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

  const exportSettings = async () => {
    const all = (await getSettings()) as Record<string, unknown>;
    const safe: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(all)) {
      if (!SENSITIVE_EXPORT.has(k)) safe[k] = v;
    }
    downloadFile(`agentx-settings-${new Date().toISOString().slice(0, 10)}.json`, JSON.stringify(safe, null, 2), "application/json");
  };

  const importSettings = async (file: File) => {
    setImportMsg(null);
    try {
      const text = await file.text();
      const parsed = JSON.parse(text) as Partial<AppSettings>;
      const current = (await getSettings()) as Partial<AppSettings>;
      // Don't overwrite secrets that aren't in the export
      const merged = { ...current, ...parsed };
      await saveSettings(merged);
      setImportMsg("✓ Settings imported. Reload to see all changes.");
    } catch (e) {
      setImportMsg(e instanceof Error ? `Import failed: ${e.message}` : "Import failed");
    }
  };

  const clearMutes = () => {
    update({ muted_symbols: [], muted_signal_types: [], snoozed_until: null });
  };

  const snoozeFor = (mins: number) => {
    const until = new Date(Date.now() + mins * 60_000).toISOString();
    update({ snoozed_until: until });
  };

  const applyRecommendedMutes = async () => {
    try {
      const edge = await api.getSignalEdge();
      const set = new Set(settings.muted_signal_types ?? []);
      edge.recommended_mutes.forEach((t) => set.add(t));
      await update({ muted_signal_types: Array.from(set) });
      setImportMsg(`✓ Muted ${edge.recommended_mutes.length} loser signal types.`);
      setTimeout(() => setImportMsg(null), 3000);
    } catch (e) {
      setImportMsg(e instanceof Error ? e.message : "Couldn't fetch recommendations");
    }
  };

  const provider = (settings.llm_provider || "gemini") as keyof typeof LLM_MODELS;
  const models = LLM_MODELS[provider] || [];

  if (loading) {
    return <div className="flex items-center justify-center h-full text-zinc-500 text-sm">Loading settings...</div>;
  }

  return (
    <div className="flex flex-col h-full">
      <div className="flex-1 overflow-y-auto px-3 py-3 space-y-4">

        {/* Broker connection */}
        <section>
          <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-2">Broker</h3>
          <BrokerPanel />
        </section>

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

        {/* Advisor Mode — risk + position sizing + regime + costs */}
        <section>
          <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-2">Advisor Mode</h3>
          <div className="space-y-3">
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="text-xs text-zinc-400 block mb-1">Trading capital (₹)</label>
                <input
                  type="number" min={0} step={1000}
                  value={settings.capital ?? 100000}
                  onChange={(e) => set("capital", Number(e.target.value))}
                  className="w-full bg-zinc-800 border border-border rounded px-2 py-1 text-xs text-zinc-100"
                />
              </div>
              <div>
                <label className="text-xs text-zinc-400 block mb-1">Risk per trade (%)</label>
                <input
                  type="number" min={0.1} max={5} step={0.1}
                  value={settings.risk_per_trade_pct ?? 1}
                  onChange={(e) => set("risk_per_trade_pct", Number(e.target.value))}
                  className="w-full bg-zinc-800 border border-border rounded px-2 py-1 text-xs text-zinc-100"
                />
                <p className="text-[10px] text-zinc-600 mt-0.5">
                  Pros: 0.5–1%. Aggressive: ≤2%.
                </p>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="text-xs text-zinc-400 block mb-1">ATR × Stop Loss</label>
                <input
                  type="number" min={0.5} max={5} step={0.1}
                  value={settings.atr_sl_mult ?? 1.5}
                  onChange={(e) => set("atr_sl_mult", Number(e.target.value))}
                  className="w-full bg-zinc-800 border border-border rounded px-2 py-1 text-xs text-zinc-100"
                />
              </div>
              <div>
                <label className="text-xs text-zinc-400 block mb-1">ATR × Target</label>
                <input
                  type="number" min={0.5} max={10} step={0.1}
                  value={settings.atr_target_mult ?? 3.0}
                  onChange={(e) => set("atr_target_mult", Number(e.target.value))}
                  className="w-full bg-zinc-800 border border-border rounded px-2 py-1 text-xs text-zinc-100"
                />
                <p className="text-[10px] text-zinc-600 mt-0.5">
                  R:R = Target ÷ SL = {((settings.atr_target_mult ?? 3) / (settings.atr_sl_mult ?? 1.5)).toFixed(2)}
                </p>
              </div>
            </div>

            <label className="flex items-center justify-between text-xs text-zinc-300">
              <span>
                Regime-aware filter
                <span className="text-[10px] text-zinc-600 block">
                  Hide breakouts in ranging markets, mean-reversion in strong trends
                </span>
              </span>
              <input
                type="checkbox"
                checked={settings.regime_filter !== false}
                onChange={(e) => set("regime_filter", e.target.checked)}
                className="accent-brand"
              />
            </label>

            <label className="flex items-center justify-between text-xs text-zinc-300">
              <span>
                Deduplicate signals
                <span className="text-[10px] text-zinc-600 block">
                  Same symbol + day + direction → keep strongest, badge the rest
                </span>
              </span>
              <input
                type="checkbox"
                checked={settings.dedupe_signals !== false}
                onChange={(e) => set("dedupe_signals", e.target.checked)}
                className="accent-brand"
              />
            </label>

            <div className="border-t border-border pt-2">
              <label className="flex items-center justify-between text-xs text-zinc-300">
                <span>
                  Autonomous paper trading
                  <span className="text-[10px] text-zinc-600 block">
                    Auto-open paper positions on high-strength signals; auto-close on SL/Target every 5 min during market hours
                  </span>
                </span>
                <input
                  type="checkbox"
                  checked={!!settings.auto_paper_trade}
                  onChange={(e) => set("auto_paper_trade", e.target.checked)}
                  className="accent-brand"
                />
              </label>
              {settings.auto_paper_trade && (
                <div className="grid grid-cols-2 gap-2 mt-2">
                  <div>
                    <label className="text-xs text-zinc-400 block mb-1">Min strength</label>
                    <input
                      type="number" min={5} max={10}
                      value={settings.auto_paper_min_strength ?? 8}
                      onChange={(e) => set("auto_paper_min_strength", Number(e.target.value))}
                      className="w-full bg-zinc-800 border border-border rounded px-2 py-1 text-xs text-zinc-100"
                    />
                  </div>
                  <div>
                    <label className="text-xs text-zinc-400 block mb-1">Max open positions</label>
                    <input
                      type="number" min={1} max={50}
                      value={settings.auto_paper_max_open ?? 10}
                      onChange={(e) => set("auto_paper_max_open", Number(e.target.value))}
                      className="w-full bg-zinc-800 border border-border rounded px-2 py-1 text-xs text-zinc-100"
                    />
                  </div>
                </div>
              )}
            </div>

            <div>
              <label className="text-xs text-zinc-400 block mb-1">Round-trip cost (%)</label>
              <input
                type="number" min={0} max={5} step={0.05}
                value={settings.roundtrip_cost_pct ?? 0.5}
                onChange={(e) => set("roundtrip_cost_pct", Number(e.target.value))}
                className="w-full bg-zinc-800 border border-border rounded px-2 py-1 text-xs text-zinc-100"
              />
              <p className="text-[10px] text-zinc-600 mt-0.5">
                Brokerage + STT + slippage. Backtest PnL is shown net of this.
              </p>
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

            {/* Layer-2 LLM judge — pairs with the keep/downgrade/drop badge on
                signal cards. Off by default; turning it on costs 1 LLM call
                per scan and may downgrade/drop deterministic candidates. */}
            <div className="border-t border-border pt-3">
              <label className="flex items-center justify-between text-xs text-zinc-300">
                <span>
                  LLM signal judging
                  <span className="text-[10px] text-zinc-600 block">
                    One batched LLM review per scan — endorses, downgrades, or drops candidates
                  </span>
                </span>
                <input
                  type="checkbox"
                  checked={
                    settings.llm_judging_enabled === true ||
                    String(settings.llm_judging_enabled).toLowerCase() === "true"
                  }
                  onChange={(e) => set("llm_judging_enabled", e.target.checked)}
                  className="accent-brand"
                />
              </label>
            </div>

            {/* Bull/Bear/Judge debate — costs more, only acts on the top-3
                strongest directional signals. Pair this with the Layer-2
                judge: judge filters individually, debate stress-tests the
                strongest survivors. */}
            <div>
              <label className="flex items-center justify-between text-xs text-zinc-300">
                <span>
                  Bull/Bear/Judge debate
                  <span className="text-[10px] text-zinc-600 block">
                    3 LLM agents debate the top 3 strong signals — judge winner flips the badge
                  </span>
                </span>
                <input
                  type="checkbox"
                  checked={
                    settings.debate_enabled === true ||
                    String(settings.debate_enabled).toLowerCase() === "true"
                  }
                  onChange={(e) => set("debate_enabled", e.target.checked)}
                  className="accent-brand"
                />
              </label>
            </div>

            {/* Multi-perspective specialist analyst — heaviest layer. */}
            <div>
              <label className="flex items-center justify-between text-xs text-zinc-300">
                <span>
                  Multi-perspective analyst
                  <span className="text-[10px] text-zinc-600 block">
                    Technical/Fundamental/Sentiment/Macro specialists on top 5 signals — slow + costly
                  </span>
                </span>
                <input
                  type="checkbox"
                  checked={
                    settings.multi_perspective_enabled === true ||
                    String(settings.multi_perspective_enabled).toLowerCase() === "true"
                  }
                  onChange={(e) => set("multi_perspective_enabled", e.target.checked)}
                  className="accent-brand"
                />
              </label>
            </div>
          </div>
        </section>

        {/* Broker integration — AngelOne SmartAPI + Kite Connect ───────── */}
        <section>
          <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-2">
            Broker (real-time data + Greeks)
          </h3>
          <div className="space-y-3">
            <div>
              <label className="text-xs text-zinc-400 block mb-1">Data source</label>
              <select
                value={settings.broker || ""}
                onChange={(e) => set("broker", e.target.value as AppSettings["broker"])}
                className="w-full bg-zinc-800 border border-border rounded px-2 py-1.5 text-sm text-zinc-100"
              >
                <option value="">yfinance (delayed, default)</option>
                <option value="angelone">AngelOne SmartAPI</option>
                <option value="kite">Zerodha Kite Connect</option>
              </select>
              <div className="text-[10px] text-zinc-500 mt-1">
                Selecting a broker enables real-time L1 quotes + native option Greeks.
                Credentials are encrypted at rest.
              </div>
            </div>

            {settings.broker === "angelone" && (
              <>
                <div>
                  <label className="text-xs text-zinc-400 block mb-1">API Key</label>
                  <input
                    type="password"
                    value={settings.angelone_api_key || ""}
                    onChange={(e) => set("angelone_api_key", e.target.value)}
                    placeholder="From smartapi.angelbroking.com"
                    className="w-full bg-zinc-800 border border-border rounded px-2 py-1.5 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none focus:border-brand"
                  />
                </div>
                <div>
                  <label className="text-xs text-zinc-400 block mb-1">Client Code</label>
                  <input
                    type="text"
                    value={settings.angelone_client_code || ""}
                    onChange={(e) => set("angelone_client_code", e.target.value)}
                    placeholder="e.g. A12345"
                    className="w-full bg-zinc-800 border border-border rounded px-2 py-1.5 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none focus:border-brand"
                  />
                </div>
                <div>
                  <label className="text-xs text-zinc-400 block mb-1">MPIN</label>
                  <input
                    type="password"
                    value={settings.angelone_mpin || ""}
                    onChange={(e) => set("angelone_mpin", e.target.value)}
                    placeholder="4-6 digit MPIN"
                    className="w-full bg-zinc-800 border border-border rounded px-2 py-1.5 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none focus:border-brand"
                  />
                </div>
                <div>
                  <label className="text-xs text-zinc-400 block mb-1">TOTP Secret</label>
                  <input
                    type="password"
                    value={settings.angelone_totp_secret || ""}
                    onChange={(e) => set("angelone_totp_secret", e.target.value)}
                    placeholder="base32 secret from broker portal"
                    className="w-full bg-zinc-800 border border-border rounded px-2 py-1.5 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none focus:border-brand"
                  />
                  <div className="text-[10px] text-zinc-500 mt-1">
                    Server uses this with pyotp to auto-generate the 2FA code at login.
                  </div>
                </div>
              </>
            )}

            {settings.broker === "kite" && (
              <>
                <div>
                  <label className="text-xs text-zinc-400 block mb-1">API Key</label>
                  <input
                    type="password"
                    value={settings.kite_api_key || ""}
                    onChange={(e) => set("kite_api_key", e.target.value)}
                    placeholder="From kite.trade developer console"
                    className="w-full bg-zinc-800 border border-border rounded px-2 py-1.5 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none focus:border-brand"
                  />
                </div>
                <div>
                  <label className="text-xs text-zinc-400 block mb-1">API Secret</label>
                  <input
                    type="password"
                    value={settings.kite_api_secret || ""}
                    onChange={(e) => set("kite_api_secret", e.target.value)}
                    placeholder="From kite.trade developer console"
                    className="w-full bg-zinc-800 border border-border rounded px-2 py-1.5 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none focus:border-brand"
                  />
                </div>
                <div>
                  <label className="text-xs text-zinc-400 block mb-1">Access Token</label>
                  <input
                    type="password"
                    value={settings.kite_access_token || ""}
                    onChange={(e) => set("kite_access_token", e.target.value)}
                    placeholder="Refresh daily after Zerodha web login"
                    className="w-full bg-zinc-800 border border-border rounded px-2 py-1.5 text-xs text-zinc-100 placeholder-zinc-600 focus:outline-none focus:border-brand"
                  />
                  <div className="text-[10px] text-zinc-500 mt-1">
                    Kite tokens expire daily ~6 AM IST. Paste the latest after logging in.
                  </div>
                </div>
              </>
            )}
          </div>
        </section>

        {/* Notifications & Audio */}
        <section>
          <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-2">Notifications</h3>
          <div className="space-y-3">
            <label className="flex items-center justify-between text-xs text-zinc-300">
              <span>Audio alerts on high-strength signals</span>
              <input
                type="checkbox"
                checked={!!settings.audio_alerts}
                onChange={(e) => set("audio_alerts", e.target.checked)}
                className="accent-brand"
              />
            </label>
            {settings.audio_alerts && (
              <div>
                <label className="text-xs text-zinc-400 block mb-1">Audio threshold (strength)</label>
                <input
                  type="range" min={5} max={10}
                  value={settings.audio_strength_threshold ?? 8}
                  onChange={(e) => set("audio_strength_threshold", Number(e.target.value))}
                  className="w-full accent-brand"
                />
                <div className="text-[10px] text-zinc-500">≥ {settings.audio_strength_threshold ?? 8}/10</div>
              </div>
            )}
            <div>
              <label className="text-xs text-zinc-400 block mb-1">Snooze all signals</label>
              <div className="flex flex-wrap gap-1.5">
                {[15, 60, 240, 1440].map((m) => (
                  <button key={m} onClick={() => snoozeFor(m)}
                    className="text-[10px] px-2 py-0.5 rounded border border-border text-zinc-300 hover:text-zinc-100">
                    {m < 60 ? `${m}m` : m < 1440 ? `${m / 60}h` : "1d"}
                  </button>
                ))}
                <button onClick={() => set("snoozed_until", null)}
                  className="text-[10px] px-2 py-0.5 rounded border border-border text-zinc-500 hover:text-zinc-300">
                  Clear
                </button>
              </div>
              {settings.snoozed_until && Date.parse(settings.snoozed_until) > Date.now() && (
                <div className="text-[10px] text-amber-400 mt-1">
                  Snoozed until {new Date(settings.snoozed_until).toLocaleString("en-IN")}
                </div>
              )}
            </div>

            {((settings.muted_symbols?.length ?? 0) > 0 || (settings.muted_signal_types?.length ?? 0) > 0) && (
              <div>
                <label className="text-xs text-zinc-400 block mb-1">Muted</label>
                <div className="flex flex-wrap gap-1">
                  {(settings.muted_symbols ?? []).map((s) => (
                    <button key={`sym-${s}`} onClick={() => set("muted_symbols", (settings.muted_symbols ?? []).filter((x) => x !== s))}
                      className="text-[10px] px-1.5 py-0.5 rounded border border-zinc-700 text-zinc-300 hover:border-loss hover:text-loss">
                      {s} ×
                    </button>
                  ))}
                  {(settings.muted_signal_types ?? []).map((t) => (
                    <button key={`type-${t}`} onClick={() => set("muted_signal_types", (settings.muted_signal_types ?? []).filter((x) => x !== t))}
                      className="text-[10px] px-1.5 py-0.5 rounded border border-zinc-700 text-zinc-300 hover:border-loss hover:text-loss">
                      {t} ×
                    </button>
                  ))}
                </div>
                <div className="flex items-center gap-2 mt-1">
                  <button onClick={clearMutes} className="text-[10px] text-zinc-500 hover:text-zinc-300">Clear all mutes</button>
                  <span className="text-zinc-700">·</span>
                  <button
                    onClick={applyRecommendedMutes}
                    className="text-[10px] text-amber-400 hover:text-amber-200"
                    title="Mute signal types that lost money in the internal backtest"
                  >
                    Apply recommended (backtest-based)
                  </button>
                </div>
              </div>
            )}
            {!((settings.muted_symbols?.length ?? 0) > 0 || (settings.muted_signal_types?.length ?? 0) > 0) && (
              <button
                onClick={applyRecommendedMutes}
                className="text-[10px] text-amber-400 hover:text-amber-200"
                title="Mute signal types that lost money in the internal backtest"
              >
                Apply recommended mutes (5 backtest losers)
              </button>
            )}
          </div>
        </section>

        {/* Telegram forwarding */}
        <section>
          <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-2">Telegram (optional)</h3>
          <div className="space-y-2">
            <p className="text-[10px] text-zinc-600 leading-relaxed">
              Forward high-strength signals to your Telegram. Create a bot via @BotFather, get your chat_id from @userinfobot.
            </p>
            <input
              type="password"
              placeholder="Bot token"
              value={settings.telegram_bot_token ?? ""}
              onChange={(e) => set("telegram_bot_token", e.target.value)}
              className="w-full bg-zinc-800 border border-border rounded px-2 py-1.5 text-xs text-zinc-100"
            />
            <input
              type="text"
              placeholder="Chat ID"
              value={settings.telegram_chat_id ?? ""}
              onChange={(e) => set("telegram_chat_id", e.target.value)}
              className="w-full bg-zinc-800 border border-border rounded px-2 py-1.5 text-xs text-zinc-100"
            />
            <div>
              <label className="text-xs text-zinc-400 block mb-1">Min strength to forward</label>
              <input
                type="range" min={5} max={10}
                value={settings.telegram_min_strength ?? 8}
                onChange={(e) => set("telegram_min_strength", Number(e.target.value))}
                className="w-full accent-brand"
              />
              <div className="text-[10px] text-zinc-500">≥ {settings.telegram_min_strength ?? 8}/10</div>
            </div>
          </div>
        </section>

        {/* Appearance */}
        <section>
          <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-2">Appearance</h3>
          <div>
            <label className="text-xs text-zinc-400 block mb-1">Theme</label>
            <div className="flex gap-1">
              {(["dark", "light"] as const).map((t) => (
                <button
                  key={t}
                  onClick={() => set("theme", t)}
                  className={`flex-1 py-1.5 text-xs rounded border capitalize ${
                    (settings.theme ?? "dark") === t
                      ? "bg-brand text-white border-brand"
                      : "border-border text-zinc-400 hover:text-zinc-200"
                  }`}
                >
                  {t}
                </button>
              ))}
            </div>
          </div>
        </section>

        {/* Data — import/export */}
        <section>
          <h3 className="text-xs font-semibold text-zinc-400 uppercase tracking-wider mb-2">Data</h3>
          <div className="flex gap-1.5">
            <button onClick={exportSettings}
              className="flex-1 text-[11px] py-1.5 rounded border border-border text-zinc-300 hover:text-zinc-100">
              Export settings (no secrets)
            </button>
            <label className="flex-1 text-[11px] text-center py-1.5 rounded border border-border text-zinc-300 hover:text-zinc-100 cursor-pointer">
              Import JSON
              <input type="file" accept=".json,application/json" className="hidden"
                onChange={(e) => { const f = e.target.files?.[0]; if (f) importSettings(f); }} />
            </label>
          </div>
          {importMsg && <div className="text-[10px] text-zinc-400 mt-1">{importMsg}</div>}
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
