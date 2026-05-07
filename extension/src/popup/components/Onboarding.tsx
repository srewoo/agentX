import { useEffect, useState } from "react";
import { api } from "../../shared/api";
import { getSettings, saveSettings } from "../../shared/storage";
import { LLM_MODELS } from "../../shared/constants";
import type { AppSettings } from "../../shared/types";

interface Props {
  onDone: () => void;
}

type Step = 0 | 1 | 2 | 3;

const SUGGESTED_SEEDS: Array<{ symbol: string; name: string }> = [
  { symbol: "RELIANCE", name: "Reliance Industries" },
  { symbol: "TCS", name: "Tata Consultancy Services" },
  { symbol: "HDFCBANK", name: "HDFC Bank" },
  { symbol: "INFY", name: "Infosys" },
  { symbol: "ICICIBANK", name: "ICICI Bank" },
  { symbol: "BHARTIARTL", name: "Bharti Airtel" },
  { symbol: "SBIN", name: "State Bank of India" },
  { symbol: "ITC", name: "ITC" },
];

export default function Onboarding({ onDone }: Props) {
  const [step, setStep] = useState<Step>(0);
  const [healthOk, setHealthOk] = useState<boolean | null>(null);
  const [healthMsg, setHealthMsg] = useState("");
  const [provider, setProvider] = useState<"gemini" | "openai" | "claude">("gemini");
  const [apiKey, setApiKey] = useState("");
  const [seeds, setSeeds] = useState<Set<string>>(
    new Set(SUGGESTED_SEEDS.slice(0, 4).map((s) => s.symbol))
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Auto-probe backend on step 0
  useEffect(() => {
    if (step !== 0) return;
    setHealthOk(null);
    setHealthMsg("Checking…");
    api.health()
      .then((h) => {
        setHealthOk(h.status === "ok");
        setHealthMsg(`Backend ok · DB ${h.db} · Cache ${h.cache} · Market ${h.market_open ? "Open" : "Closed"}`);
      })
      .catch((e) => {
        setHealthOk(false);
        setHealthMsg(e instanceof Error ? e.message : "Backend unreachable");
      });
  }, [step]);

  const finish = async () => {
    setBusy(true);
    setError(null);
    try {
      const current = (await getSettings()) as Partial<AppSettings>;
      // Pre-seed muted signal types with the backend's recommended mutes
      // (signals that lost money in the internal backtest). Users can
      // un-mute any of them later from Settings.
      let recommendedMutes: string[] = [];
      try {
        const edge = await api.getSignalEdge();
        recommendedMutes = edge.recommended_mutes ?? [];
      } catch { /* backend offline — ship empty mute list */ }
      const existingMutes = new Set(current.muted_signal_types ?? []);
      recommendedMutes.forEach((t) => existingMutes.add(t));

      const next: Partial<AppSettings> = {
        ...current,
        llm_provider: provider,
        llm_model: LLM_MODELS[provider]?.[0] || current.llm_model,
        onboarding_complete: true,
        muted_signal_types: Array.from(existingMutes),
      };
      const keyField = `${provider}_api_key` as keyof AppSettings;
      if (apiKey) (next as Record<string, unknown>)[keyField] = apiKey;
      await saveSettings(next);

      // Seed watchlist (best-effort, ignore individual failures)
      await Promise.all(
        Array.from(seeds).map((sym) => {
          const s = SUGGESTED_SEEDS.find((x) => x.symbol === sym);
          return api.addToWatchlist(sym, s?.name || sym).catch(() => null);
        })
      );
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const skipAll = async () => {
    const current = (await getSettings()) as Partial<AppSettings>;
    await saveSettings({ ...current, onboarding_complete: true });
    onDone();
  };

  return (
    <div className="absolute inset-0 z-50 bg-zinc-950/95 backdrop-blur flex flex-col p-5">
      {/* progress dots */}
      <div className="flex items-center justify-center gap-2 mb-5">
        {[0, 1, 2, 3].map((i) => (
          <div
            key={i}
            className={`h-1.5 rounded-full transition-all ${
              i === step ? "w-6 bg-brand" : i < step ? "w-3 bg-brand/60" : "w-3 bg-zinc-700"
            }`}
          />
        ))}
      </div>

      <div className="flex-1 overflow-y-auto">
        {step === 0 && (
          <div className="space-y-4">
            <div className="text-center">
              <div className="text-3xl mb-2">📈</div>
              <h2 className="text-lg font-bold text-zinc-100">Welcome to agentX</h2>
              <p className="text-xs text-zinc-400 mt-1">Your AI-powered NSE/BSE trading copilot</p>
            </div>
            <div className={`p-3 rounded-lg border ${
              healthOk === true ? "border-emerald-500/40 bg-emerald-500/10"
                : healthOk === false ? "border-red-500/40 bg-red-500/10"
                : "border-zinc-700 bg-zinc-800/50"
            }`}>
              <div className="text-xs font-medium text-zinc-300 mb-1">Backend connection</div>
              <div className="text-[11px] text-zinc-400">{healthMsg}</div>
              {healthOk === false && (
                <div className="text-[11px] text-zinc-500 mt-2">
                  Run <code className="text-brand-light">./start.sh</code> in the project root, then click Retry.
                </div>
              )}
            </div>
            <div className="text-[11px] text-zinc-500 leading-relaxed">
              agentX scans NSE/BSE for technical setups, runs AI analysis on demand, and surfaces signals with FII/DII + India VIX context.
              No broker integration — purely research and decision support.
            </div>
          </div>
        )}

        {step === 1 && (
          <div className="space-y-4">
            <div>
              <h2 className="text-base font-bold text-zinc-100">Add your AI key</h2>
              <p className="text-[11px] text-zinc-500 mt-1">Used for on-demand analysis. Keys stay local — never sent anywhere except the chosen provider.</p>
            </div>
            <div>
              <label className="text-xs text-zinc-400 block mb-1.5">Provider</label>
              <div className="flex gap-1.5">
                {(["gemini", "openai", "claude"] as const).map((p) => (
                  <button
                    key={p}
                    onClick={() => setProvider(p)}
                    className={`flex-1 py-2 text-xs rounded border capitalize ${
                      provider === p
                        ? "bg-brand text-white border-brand"
                        : "border-border text-zinc-400 hover:text-zinc-200"
                    }`}
                  >
                    {p === "gemini" ? "Gemini" : p === "openai" ? "OpenAI" : "Claude"}
                  </button>
                ))}
              </div>
            </div>
            <div>
              <label className="text-xs text-zinc-400 block mb-1.5">{provider === "gemini" ? "Gemini" : provider === "openai" ? "OpenAI" : "Claude"} API key</label>
              <input
                type="password"
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
                placeholder={`Paste your ${provider} key…`}
                className="w-full bg-zinc-800 border border-border rounded-lg px-3 py-2 text-xs text-zinc-100 focus:outline-none focus:border-brand"
              />
              <p className="text-[10px] text-zinc-600 mt-1.5">
                {provider === "gemini" && "Get a free key at aistudio.google.com/apikey"}
                {provider === "openai" && "Get a key at platform.openai.com/api-keys"}
                {provider === "claude" && "Get a key at console.anthropic.com"}
              </p>
            </div>
          </div>
        )}

        {step === 2 && (
          <div className="space-y-4">
            <div>
              <h2 className="text-base font-bold text-zinc-100">Seed your watchlist</h2>
              <p className="text-[11px] text-zinc-500 mt-1">Pick the stocks you care about — we'll prioritize signals on these.</p>
            </div>
            <div className="grid grid-cols-2 gap-1.5">
              {SUGGESTED_SEEDS.map((s) => {
                const on = seeds.has(s.symbol);
                return (
                  <button
                    key={s.symbol}
                    onClick={() => {
                      const next = new Set(seeds);
                      if (on) next.delete(s.symbol); else next.add(s.symbol);
                      setSeeds(next);
                    }}
                    className={`text-left px-2.5 py-2 rounded border text-xs transition-colors ${
                      on ? "border-brand/50 bg-brand/15 text-zinc-100" : "border-border bg-zinc-800/50 text-zinc-400"
                    }`}
                  >
                    <div className="font-semibold">{s.symbol}</div>
                    <div className="text-[10px] text-zinc-500 truncate">{s.name}</div>
                  </button>
                );
              })}
            </div>
            <p className="text-[10px] text-zinc-600 text-center">You can edit, group, or import a CSV from the Watchlist tab later.</p>
          </div>
        )}

        {step === 3 && (
          <div className="space-y-4 text-center">
            <div className="text-4xl">✨</div>
            <h2 className="text-base font-bold text-zinc-100">You're ready.</h2>
            <ul className="text-[11px] text-zinc-400 text-left space-y-1.5 max-w-[280px] mx-auto">
              <li>• <span className="text-brand-light">Signals</span> — auto-scans during market hours</li>
              <li>• <span className="text-brand-light">Search</span> — quote + AI analysis on any NSE/BSE stock</li>
              <li>• <span className="text-brand-light">Screener</span> — presets + custom filters</li>
              <li>• <span className="text-brand-light">Alerts</span> — price triggers</li>
              <li>• Right-click a ticker on any web page to analyze it instantly</li>
              <li className="text-zinc-500 text-[10px] pt-1">
                · Auto-muting 5 signal types that lost money in the internal backtest
                (manageable in Settings).
              </li>
            </ul>
            {error && <div className="text-xs text-loss">{error}</div>}
          </div>
        )}
      </div>

      {/* Footer nav */}
      <div className="flex items-center justify-between pt-4 border-t border-border mt-3">
        <button
          onClick={skipAll}
          className="text-xs text-zinc-500 hover:text-zinc-300"
        >
          Skip
        </button>
        <div className="flex gap-2">
          {step > 0 && (
            <button
              onClick={() => setStep((s) => (s - 1) as Step)}
              className="px-3 py-1.5 text-xs rounded-lg border border-border text-zinc-300 hover:bg-zinc-800"
            >
              Back
            </button>
          )}
          {step === 0 && (
            <>
              {healthOk === false && (
                <button
                  onClick={() => setStep(0)}
                  className="px-3 py-1.5 text-xs rounded-lg border border-border text-zinc-300 hover:bg-zinc-800"
                >
                  Retry
                </button>
              )}
              <button
                onClick={() => setStep(1)}
                className="px-3 py-1.5 text-xs rounded-lg bg-brand text-white hover:bg-brand/80"
              >
                Continue
              </button>
            </>
          )}
          {step === 1 && (
            <button
              onClick={() => setStep(2)}
              className="px-3 py-1.5 text-xs rounded-lg bg-brand text-white hover:bg-brand/80"
            >
              {apiKey ? "Continue" : "Skip key for now"}
            </button>
          )}
          {step === 2 && (
            <button
              onClick={() => setStep(3)}
              className="px-3 py-1.5 text-xs rounded-lg bg-brand text-white hover:bg-brand/80"
            >
              Continue
            </button>
          )}
          {step === 3 && (
            <button
              onClick={finish}
              disabled={busy}
              className="px-4 py-1.5 text-xs rounded-lg bg-brand text-white hover:bg-brand/80 disabled:opacity-50"
            >
              {busy ? "Setting up…" : "Get started"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
