// AlertsManager
// ---------------------------------------------------------------------------
// Top-level alerts UI:
//   • header with count + "Create alert" CTA
//   • list grouped by symbol (collapsible groups)
//   • empty state
//   • inline confirmation for delete (no nested modals — keeps focus model
//     simple inside the popup)

import { useMemo, useState } from "react";
import { Button, Toast, type ToastState } from "../settings/_primitives";
import AlertCard from "./AlertCard";
import CreateAlertDialog from "./CreateAlertDialog";
import type {
  Alert,
  AlertChannel,
  AlertDraft,
} from "./_types";

interface Props {
  alerts: Alert[];
  enabledChannels: AlertChannel[];
  onCreate: (draft: AlertDraft) => Promise<Alert>;
  onToggle: (id: string, enabled: boolean) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
  onSendTest: (
    alertId: string,
    channel: AlertChannel,
  ) => Promise<{ ok: boolean; message: string }>;
}

export default function AlertsManager({
  alerts,
  enabledChannels,
  onCreate,
  onToggle,
  onDelete,
  onSendTest,
}: Props) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [toast, setToast] = useState<ToastState | null>(null);

  // Group alerts by symbol for scannability when the list grows.
  const groups = useMemo(() => {
    const map = new Map<string, Alert[]>();
    for (const a of alerts) {
      const list = map.get(a.symbol) ?? [];
      list.push(a);
      map.set(a.symbol, list);
    }
    return Array.from(map.entries()).sort(([a], [b]) => a.localeCompare(b));
  }, [alerts]);

  const wrappedToggle = async (id: string, enabled: boolean) => {
    try {
      await onToggle(id, enabled);
      setToast({
        kind: "success",
        message: enabled ? "Alert enabled." : "Alert disabled.",
      });
    } catch (e) {
      setToast({
        kind: "error",
        message: e instanceof Error ? e.message : "Failed to update alert.",
      });
    }
  };

  const wrappedDelete = async (id: string) => {
    try {
      await onDelete(id);
      setToast({ kind: "success", message: "Alert deleted." });
    } catch (e) {
      setToast({
        kind: "error",
        message: e instanceof Error ? e.message : "Failed to delete alert.",
      });
    }
  };

  const wrappedCreate = async (draft: AlertDraft) => {
    try {
      const result = await onCreate(draft);
      setToast({
        kind: "success",
        message: `Alert created for ${draft.symbol}.`,
      });
      return result;
    } catch (e) {
      // Re-throw so the dialog can show its own error too.
      throw e;
    }
  };

  return (
    <section aria-label="Alerts" className="flex flex-col gap-3">
      <header className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-100">
          Alerts {alerts.length > 0 ? `(${alerts.length})` : ""}
        </h2>
        <Button
          variant="primary"
          onClick={() => setDialogOpen(true)}
          disabled={enabledChannels.length === 0}
          title={
            enabledChannels.length === 0
              ? "Enable at least one notification channel first"
              : undefined
          }
        >
          + Create alert
        </Button>
      </header>

      {enabledChannels.length === 0 ? (
        <p
          role="note"
          className="rounded-md border border-amber-900/60 bg-amber-950/30 px-2.5 py-1.5 text-xs text-amber-200"
        >
          No notification channels are enabled. Open Settings → Channels to set
          one up before creating alerts.
        </p>
      ) : null}

      {alerts.length === 0 ? (
        <EmptyState />
      ) : (
        <ul className="flex flex-col gap-3" aria-label="Alerts grouped by symbol">
          {groups.map(([symbol, list]) => (
            <li key={symbol}>
              <SymbolGroup
                symbol={symbol}
                alerts={list}
                onToggle={wrappedToggle}
                onDelete={wrappedDelete}
                onSendTest={onSendTest}
              />
            </li>
          ))}
        </ul>
      )}

      <CreateAlertDialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        onCreate={wrappedCreate}
        enabledChannels={enabledChannels}
      />

      <Toast toast={toast} onDismiss={() => setToast(null)} />
    </section>
  );
}

function EmptyState() {
  return (
    <div className="rounded-md border border-dashed border-slate-700 p-4 text-center">
      <p className="text-sm text-slate-300">No alerts yet.</p>
      <p className="mt-1 text-xs text-slate-500">
        Create one to get notified when a stock crosses a price, breaks out, or
        spikes in volume.
      </p>
    </div>
  );
}

function SymbolGroup({
  symbol,
  alerts,
  onToggle,
  onDelete,
  onSendTest,
}: {
  symbol: string;
  alerts: Alert[];
  onToggle: (id: string, enabled: boolean) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
  onSendTest: (
    alertId: string,
    channel: AlertChannel,
  ) => Promise<{ ok: boolean; message: string }>;
}) {
  const [open, setOpen] = useState(true);
  const enabledCount = alerts.filter((a) => a.enabled).length;

  return (
    <div className="rounded-md border border-slate-800 bg-slate-950/40">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="w-full flex items-center justify-between px-3 py-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400 rounded-t-md"
      >
        <span className="text-sm font-mono text-slate-100">{symbol}</span>
        <span className="text-xs text-slate-400">
          {enabledCount}/{alerts.length} active{" "}
          <span aria-hidden="true">{open ? "▾" : "▸"}</span>
        </span>
      </button>
      {open ? (
        <div className="px-3 pb-3 pt-1 flex flex-col gap-2">
          {alerts.map((a) => (
            <AlertCard
              key={a.id}
              alert={a}
              onToggle={onToggle}
              onDelete={onDelete}
              onSendTest={onSendTest}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}
