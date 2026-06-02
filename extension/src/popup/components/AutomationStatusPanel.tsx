import { useEffect, useState } from "react";
import { api } from "../../shared/api";
import type { AutomationStatus } from "../../shared/types";

function ago(iso?: string | null): string {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "never";
  const secs = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 48) return `${hrs}h ago`;
  return `${Math.round(hrs / 24)}d ago`;
}

function when(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });
}

function Dot({ on }: { on: boolean }) {
  return (
    <span className={`inline-block w-2 h-2 rounded-full ${on ? "bg-emerald-500" : "bg-zinc-600"}`} />
  );
}

export default function AutomationStatusPanel() {
  const [status, setStatus] = useState<AutomationStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.getAutomationStatus();
      setStatus(res.data);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load status");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const hb = status?.heartbeats ?? {};
  const autoPaper = hb["auto_paper"];
  const scan = hb["scan"];
  const dailyBt = hb["backtest_daily"];

  return (
    <div className="space-y-2 text-xs">
      <div className="flex items-center justify-between">
        <span className="text-zinc-400">
          {status?.market_open ? "Market open" : "Market closed"}
        </span>
        <button
          onClick={load}
          disabled={loading}
          className="text-[10px] text-brand hover:underline disabled:opacity-50"
        >
          {loading ? "…" : "Refresh"}
        </button>
      </div>

      {error && <p className="text-rose-400 text-[11px]">{error}</p>}

      {status && (
        <div className="space-y-1.5 bg-zinc-800/50 rounded p-2">
          <Row label="Engine running">
            <Dot on={status.orchestrator_running} />
          </Row>
          <Row label="Auto paper-trade">
            <span className="flex items-center gap-1.5">
              <Dot on={status.auto_paper_enabled} />
              <span className="text-zinc-500">ran {ago(autoPaper?.last_run_at)}</span>
            </span>
          </Row>
          {autoPaper?.summary && (
            <p className="text-[10px] text-zinc-600 pl-1">
              last: opened {String((autoPaper.summary as Record<string, unknown>).opened ?? 0)},
              closed {String((autoPaper.summary as Record<string, unknown>).closed ?? 0)}
            </p>
          )}
          <Row label="Scan loop">
            <span className="text-zinc-500">ran {ago(scan?.last_run_at)}</span>
          </Row>
          <Row label="Open positions">
            <span className="text-zinc-300">{status.open_positions}</span>
          </Row>
          <div className="border-t border-border my-1" />
          <Row label="Daily backtest (11:00 IST)">
            <span className="flex items-center gap-1.5">
              <Dot on={status.daily_backtest_enabled} />
              <span className="text-zinc-500">ran {ago(dailyBt?.last_run_at)}</span>
            </span>
          </Row>
          <Row label="Last backtest">
            <span className="text-zinc-500">{ago(status.last_backtest_at)}</span>
          </Row>
          <Row label="Next daily / weekly">
            <span className="text-zinc-500">
              {when(status.next_daily_backtest_utc)} · {when(status.next_weekly_backtest_utc)}
            </span>
          </Row>
        </div>
      )}

      {!status?.orchestrator_running && status && (
        <p className="text-[10px] text-amber-500/80">
          Engine isn't running — the backend must stay up during market hours for
          auto paper-trading and scans to fire.
        </p>
      )}
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between">
      <span className="text-zinc-400">{label}</span>
      {children}
    </div>
  );
}
