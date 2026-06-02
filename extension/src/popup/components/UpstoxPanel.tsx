import { useState } from "react";
import { api } from "../../shared/api";
import { useSettings } from "../hooks/useSettings";

/**
 * UpstoxPanel — Settings widget for the Upstox data source.
 *
 * Upstox is the authenticated PRIMARY market-data source (daily OHLCV,
 * quotes, option chain). Unlike the NSE/yfinance scrapers it is not subject
 * to anti-bot 403s. Tokens are issued via Upstox OAuth and expire ~03:30 IST
 * daily, so this is a paste-the-daily-token field (same UX as Kite).
 *
 *   • Save token  → POST /api/settings (sealed at rest, never echoed back)
 *   • Test        → POST /api/settings/test-upstox (validates against Upstox)
 */
export default function UpstoxPanel() {
  const { settings, update } = useSettings();
  const [token, setToken] = useState("");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const configured = Boolean(settings.upstox_access_token_configured);

  async function saveToken() {
    if (!token.trim()) return;
    setSaving(true);
    setMsg(null);
    try {
      await update({ upstox_access_token: token.trim() });
      setToken("");
      setMsg("✓ Token saved. Click Test to verify.");
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "save failed");
    } finally {
      setSaving(false);
    }
  }

  async function test() {
    setTesting(true);
    setMsg(null);
    try {
      const r = await api.testUpstox();
      setMsg(`${r.ok ? "✓" : "✗"} ${r.message}`);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "test failed");
    } finally {
      setTesting(false);
    }
  }

  return (
    <div className="space-y-2 text-[11px]">
      <div className="border border-border rounded p-2 bg-zinc-900/40">
        <div className="flex justify-between">
          <span className="text-zinc-500 uppercase tracking-wider text-[10px]">
            Upstox (primary data)
          </span>
          <span className={configured ? "text-emerald-400" : "text-zinc-500"}>
            {configured ? "token saved" : "not set"}
          </span>
        </div>
        <p className="text-zinc-500 mt-1 leading-snug">
          Authenticated source — avoids NSE 403s. Token expires daily (~03:30 IST);
          re-paste each trading day.
        </p>
      </div>

      <input
        type="password"
        value={token}
        onChange={(e) => setToken(e.target.value)}
        placeholder={configured ? "paste a fresh daily access token" : "Upstox access_token"}
        className="w-full bg-zinc-900 border border-border rounded px-2 py-1 text-zinc-200"
      />
      <div className="flex gap-2">
        <button
          onClick={saveToken}
          disabled={saving || !token.trim()}
          className="flex-1 bg-zinc-800 hover:bg-zinc-700 disabled:opacity-50 text-zinc-200 rounded px-2 py-1"
        >
          {saving ? "Saving…" : "Save token"}
        </button>
        <button
          onClick={test}
          disabled={testing}
          className="flex-1 bg-indigo-700 hover:bg-indigo-600 disabled:opacity-50 text-zinc-100 rounded px-2 py-1"
        >
          {testing ? "Testing…" : "Test connection"}
        </button>
      </div>

      {msg && <div className="text-zinc-300">{msg}</div>}
    </div>
  );
}
