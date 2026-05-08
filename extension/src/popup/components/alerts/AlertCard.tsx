// AlertCard
// ---------------------------------------------------------------------------
// One alert at a glance:
//   • symbol + condition summary (uses previewCondition for natural-language)
//   • toggle to enable/disable
//   • per-channel delivery indicator (✓ / ✗ + when)
//   • "Send test" per channel
//   • Delete (with confirmation handled by the parent — keep this dumb)

import { Switch, Button } from "../settings/_primitives";
import SendTestButton from "./SendTestButton";
import { previewCondition } from "./AlertConditionBuilder";
import type { Alert, AlertChannel } from "./_types";

interface Props {
  alert: Alert;
  onToggle: (id: string, enabled: boolean) => Promise<void>;
  onDelete: (id: string) => Promise<void>;
  onSendTest: (
    alertId: string,
    channel: AlertChannel,
  ) => Promise<{ ok: boolean; message: string }>;
}

const CHANNEL_LABELS: Record<AlertChannel, string> = {
  telegram: "Telegram",
  email: "Email",
  whatsapp: "WhatsApp",
  sms: "SMS",
};

export default function AlertCard({ alert, onToggle, onDelete, onSendTest }: Props) {
  const lastByChannel = new Map(
    alert.lastDeliveries.map((d) => [d.channel, d]),
  );

  return (
    <article
      aria-label={`Alert for ${alert.symbol}`}
      className="rounded-md border border-slate-800 bg-slate-900/60 p-3 flex flex-col gap-2"
    >
      <header className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <h4 className="text-sm font-semibold text-slate-100 font-mono">
            {alert.symbol}
          </h4>
          <p className="text-xs text-slate-300 mt-0.5">
            {previewCondition(alert.symbol, alert.condition)}
          </p>
          {alert.note ? (
            <p className="text-xs text-slate-500 mt-0.5 italic">
              Note: {alert.note}
            </p>
          ) : null}
        </div>
        <Switch
          label={`Enable alert for ${alert.symbol}`}
          checked={alert.enabled}
          onChange={(next) => void onToggle(alert.id, next)}
        />
      </header>

      {alert.channels.length > 0 ? (
        <ul
          aria-label="Channel delivery status"
          className="flex flex-wrap gap-2"
        >
          {alert.channels.map((ch) => {
            const last = lastByChannel.get(ch);
            return (
              <li
                key={ch}
                className="inline-flex items-center gap-1.5 rounded-full border border-slate-800 bg-slate-900 px-2 py-0.5 text-xs"
              >
                <span className="text-slate-300">{CHANNEL_LABELS[ch]}</span>
                {last ? (
                  <span
                    className={
                      last.status === "ok"
                        ? "text-emerald-400"
                        : "text-rose-400"
                    }
                    aria-label={
                      last.status === "ok"
                        ? `Last delivery succeeded at ${last.at}`
                        : `Last delivery failed at ${last.at}`
                    }
                    title={new Date(last.at).toLocaleString()}
                  >
                    {last.status === "ok" ? "✓" : "✗"}
                  </span>
                ) : (
                  <span
                    className="text-slate-500"
                    aria-label="No deliveries yet"
                  >
                    ·
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="text-xs text-amber-400">
          No channels selected — this alert won't notify you.
        </p>
      )}

      <footer className="flex flex-wrap items-center justify-between gap-2 mt-1">
        <div className="flex flex-wrap gap-1">
          {alert.channels.map((ch) => (
            <SendTestButton
              key={ch}
              alertId={alert.id}
              channel={ch}
              onSendTest={onSendTest}
              size="sm"
            />
          ))}
        </div>
        <Button
          variant="danger"
          onClick={() => void onDelete(alert.id)}
          aria-label={`Delete alert for ${alert.symbol}`}
          className="text-xs px-2 py-1"
        >
          Delete
        </Button>
      </footer>
    </article>
  );
}
