import { useEffect, useState } from "react";
import { api } from "../../shared/api";

/**
 * BrokerPanel — Settings widget for broker connection.
 *
 * Shows current broker, connection status, and:
 *   • Test connection button (calls /api/broker/test)
 *   • Sign in with Zerodha (opens Kite OAuth URL in a new tab; user
 *     pastes the redirected request_token back)
 */
export default function BrokerPanel() {
  const [status, setStatus] = useState<Awaited<ReturnType<typeof api.getBrokerStatus>> | null>(null);
  const [testing, setTesting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [requestToken, setRequestToken] = useState("");
  const [exchanging, setExchanging] = useState(false);

  async function refresh() {
    try { setStatus(await api.getBrokerStatus()); } catch (e) { setMsg(e instanceof Error ? e.message : String(e)); }
  }

  useEffect(() => { refresh(); }, []);

  async function test() {
    setTesting(true); setMsg(null);
    try {
      const r = await api.testBroker();
      setMsg(`${r.ok ? "✓" : "✗"} ${r.broker}: ${r.message}`);
      await refresh();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "test failed");
    } finally {
      setTesting(false);
    }
  }

  async function openKiteLogin() {
    try {
      const r = await api.getKiteLoginUrl();
      window.open(r.login_url, "_blank", "noopener,noreferrer");
      setMsg("Opened Zerodha login. After redirect, paste the request_token below.");
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "could not start Kite login");
    }
  }

  async function exchangeToken() {
    if (!requestToken.trim()) return;
    setExchanging(true); setMsg(null);
    try {
      const r = await api.kiteExchangeToken(requestToken.trim());
      setMsg(`✓ ${r.message}`);
      setRequestToken("");
      await refresh();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "exchange failed");
    } finally {
      setExchanging(false);
    }
  }

  return (
    <div className="space-y-2 text-[11px]">
      <div className="border border-border rounded p-2 bg-zinc-900/40">
        <div className="flex justify-between">
          <span className="text-zinc-500 uppercase tracking-wider text-[10px]">Broker</span>
          <span className="text-zinc-300">{status?.broker ?? "—"}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-zinc-500">Credentials</span>
          <span className={status?.credentials_present ? "text-emerald-400" : "text-zinc-500"}>
            {status?.credentials_present ? "present" : "missing"}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-zinc-500">Last check</span>
          <span className={status?.last_check_ok ? "text-emerald-400" : "text-zinc-400"}>
            {status?.last_check_iso ? new Date(status.last_check_iso).toLocaleString() : "never"}
          </span>
        </div>
      </div>
      <button
        onClick={test}
        disabled={testing}
        className="w-full bg-zinc-800 hover:bg-zinc-700 disabled:opacity-50 text-zinc-200 rounded px-2 py-1 text-[11px]"
      >
        {testing ? "Testing…" : "Test connection"}
      </button>

      <div className="border border-border rounded p-2 bg-zinc-900/40 space-y-1">
        <div className="text-zinc-500 uppercase tracking-wider text-[10px]">Kite (Zerodha) OAuth</div>
        <button
          onClick={openKiteLogin}
          className="w-full bg-emerald-700 hover:bg-emerald-600 text-zinc-100 rounded px-2 py-1"
        >
          Sign in with Zerodha
        </button>
        <input
          value={requestToken}
          onChange={(e) => setRequestToken(e.target.value)}
          placeholder="paste request_token from Kite redirect"
          className="w-full bg-zinc-900 border border-border rounded px-2 py-1 text-zinc-200"
        />
        <button
          onClick={exchangeToken}
          disabled={exchanging || !requestToken.trim()}
          className="w-full bg-zinc-800 hover:bg-zinc-700 disabled:opacity-50 text-zinc-200 rounded px-2 py-1"
        >
          {exchanging ? "Exchanging…" : "Exchange for access_token"}
        </button>
      </div>

      {msg && <div className="text-zinc-300">{msg}</div>}
    </div>
  );
}
