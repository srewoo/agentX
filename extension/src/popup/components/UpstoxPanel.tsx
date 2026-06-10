import { useState } from "react";
import { api } from "../../shared/api";
import { useSettings } from "../hooks/useSettings";

/**
 * UpstoxPanel — Settings widget for the Upstox data source.
 *
 * Upstox is the authenticated PRIMARY market-data source (daily OHLCV,
 * quotes, option chain). Unlike the NSE/yfinance scrapers it is not subject
 * to anti-bot 403s.
 *
 * PRIMARY path: paste an Upstox **Analytics Token** (Developer Apps → Analytics
 * tab → Generate Token). It is market-data-scoped, valid ~1 year, and is what
 * the fetcher sends as the Bearer token. Saved to `upstox_access_token`.
 *
 * LEGACY path (collapsed): the daily OAuth authorization-code flow. It yields a
 * standard access token that authenticates account endpoints but 401s on
 * market-quote endpoints — kept only for callers who still need account-scoped
 * auth. Do NOT use it for data.
 *
 *   • Save token  → POST /api/settings (sealed at rest, never echoed back)
 *   • Test        → POST /api/settings/test-upstox (validates against an LTP quote)
 */
export default function UpstoxPanel() {
  const { settings, update } = useSettings();
  const [token, setToken] = useState("");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  // OAuth "generate token" flow state.
  const [showOauth, setShowOauth] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [redirectUri, setRedirectUri] = useState("https://127.0.0.1");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);

  const configured = Boolean(settings.upstox_access_token_configured);
  // Configured = backend `_configured` flag (set on reload) OR the value is
  // present in local state (set immediately after Save app creds). The hook's
  // optimistic update merges the raw values but not the flags, so without the
  // value-fallback the "Get login link" button stays disabled until a reload.
  const credsConfigured =
    Boolean(settings.upstox_api_key_configured || settings.upstox_api_key) &&
    Boolean(settings.upstox_api_secret_configured || settings.upstox_api_secret);

  async function saveCreds() {
    if (!apiKey.trim() || !apiSecret.trim()) return;
    setBusy(true);
    setMsg(null);
    try {
      await update({ upstox_api_key: apiKey.trim(), upstox_api_secret: apiSecret.trim() });
      setApiKey("");
      setApiSecret("");
      setMsg("✓ App credentials saved. Now get the login link.");
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "save failed");
    } finally {
      setBusy(false);
    }
  }

  async function openLogin() {
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.upstoxLoginUrl(redirectUri.trim());
      if (r.ok && r.url) {
        window.open(r.url, "_blank", "noopener");
        setMsg("Approve in the opened tab, then copy the ?code= value from the redirect URL.");
      } else {
        setMsg(`✗ ${r.message ?? "could not build login URL"}`);
      }
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "login URL failed");
    } finally {
      setBusy(false);
    }
  }

  async function exchange() {
    if (!code.trim()) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await api.upstoxExchangeCode(code.trim(), redirectUri.trim());
      setMsg(`${r.ok ? "✓" : "✗"} ${r.message}`);
      if (r.ok) setCode("");
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "exchange failed");
    } finally {
      setBusy(false);
    }
  }

  async function saveToken() {
    if (!token.trim()) return;
    setSaving(true);
    setMsg(null);
    try {
      await update({ upstox_access_token: token.trim() });
      setToken("");
      setMsg("✓ Analytics Token saved and now in use. Click Test to verify live data.");
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
          Authenticated source — avoids NSE 403s. Paste an{" "}
          <span className="text-zinc-300">Analytics Token</span> (Upstox Developer
          Apps → Analytics tab → Generate Token) — it's valid ~1 year and grants
          market-data access. The daily OAuth token below does <em>not</em> work
          for quotes.
        </p>
      </div>

      <input
        type="password"
        value={token}
        onChange={(e) => setToken(e.target.value)}
        placeholder={configured ? "paste a new Analytics Token to replace" : "Upstox Analytics Token"}
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

      <button
        onClick={() => setShowOauth((v) => !v)}
        className="w-full text-left text-zinc-500 hover:text-zinc-300 text-[10px] underline"
      >
        {showOauth ? "▾ Hide legacy OAuth flow" : "▸ Legacy: daily OAuth token (account auth only — not market data)"}
      </button>

      {showOauth && (
        <div className="border border-amber-900/50 rounded p-2 bg-amber-950/20 space-y-2">
          <p className="text-amber-500/90 leading-snug">
            ⚠ This produces a daily OAuth token that 401s on market-quote
            endpoints. Use the Analytics Token field above for data. Kept only
            for account-scoped auth.
          </p>
          <p className="text-zinc-500 leading-snug">
            1. Save your Upstox app's API key + secret (from the Upstox developer
            console). The redirect URL must match the one registered on the app.
          </p>
          <span className={credsConfigured ? "text-emerald-400" : "text-zinc-500"}>
            {credsConfigured ? "app credentials saved" : "app credentials not set"}
          </span>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="upstox_api_key"
            className="w-full bg-zinc-900 border border-border rounded px-2 py-1 text-zinc-200"
          />
          <input
            type="password"
            value={apiSecret}
            onChange={(e) => setApiSecret(e.target.value)}
            placeholder="upstox_api_secret"
            className="w-full bg-zinc-900 border border-border rounded px-2 py-1 text-zinc-200"
          />
          <input
            type="text"
            value={redirectUri}
            onChange={(e) => setRedirectUri(e.target.value)}
            placeholder="redirect URI (must match the Upstox app)"
            className="w-full bg-zinc-900 border border-border rounded px-2 py-1 text-zinc-200"
          />
          <div className="flex gap-2">
            <button
              onClick={saveCreds}
              disabled={busy || !apiKey.trim() || !apiSecret.trim()}
              className="flex-1 bg-zinc-800 hover:bg-zinc-700 disabled:opacity-50 text-zinc-200 rounded px-2 py-1"
            >
              Save app creds
            </button>
            <button
              onClick={openLogin}
              disabled={busy || !credsConfigured}
              className="flex-1 bg-zinc-800 hover:bg-zinc-700 disabled:opacity-50 text-zinc-200 rounded px-2 py-1"
            >
              Get login link
            </button>
          </div>
          <p className="text-zinc-500 leading-snug">
            2. After approving, Upstox redirects to your redirect URI with a
            <code className="text-zinc-300"> ?code=…</code> — paste that code here.
          </p>
          <input
            type="text"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="authorization code"
            className="w-full bg-zinc-900 border border-border rounded px-2 py-1 text-zinc-200"
          />
          <button
            onClick={exchange}
            disabled={busy || !code.trim()}
            className="w-full bg-indigo-700 hover:bg-indigo-600 disabled:opacity-50 text-zinc-100 rounded px-2 py-1"
          >
            {busy ? "Working…" : "Generate & save token"}
          </button>
        </div>
      )}

      {msg && <div className="text-zinc-300">{msg}</div>}
    </div>
  );
}
