// SendTestButton
// ---------------------------------------------------------------------------
// Standalone "send test" button that calls /api/alerts/test for a specific
// alert + channel combo. Mirrors the inline test on each ChannelsForm card
// but operates on a saved alert (so the message resembles the real payload).

import { useState } from "react";
import { Button } from "../settings/_primitives";
import type { AlertChannel } from "./_types";

interface Props {
  alertId: string;
  channel: AlertChannel;
  onSendTest: (
    alertId: string,
    channel: AlertChannel,
  ) => Promise<{ ok: boolean; message: string }>;
  // Optional small variant for cramped UIs.
  size?: "sm" | "md";
}

const CHANNEL_LABELS: Record<AlertChannel, string> = {
  telegram: "Telegram",
  email: "Email",
  whatsapp: "WhatsApp",
  sms: "SMS",
};

export default function SendTestButton({
  alertId,
  channel,
  onSendTest,
  size = "md",
}: Props) {
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(
    null,
  );

  const handleClick = async () => {
    setBusy(true);
    setResult(null);
    try {
      setResult(await onSendTest(alertId, channel));
    } catch (e) {
      setResult({
        ok: false,
        message: e instanceof Error ? e.message : "Test failed.",
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <span className="inline-flex items-center gap-2">
      <Button
        variant="ghost"
        loading={busy}
        onClick={handleClick}
        aria-label={`Send a test ${CHANNEL_LABELS[channel]} alert`}
        className={size === "sm" ? "px-2 py-0.5 text-xs" : ""}
      >
        Test {CHANNEL_LABELS[channel]}
      </Button>
      {result ? (
        <span
          role="status"
          className={`text-xs ${
            result.ok ? "text-emerald-400" : "text-rose-400"
          }`}
        >
          {result.ok ? "✓ " : "✗ "}
          {result.message}
        </span>
      ) : null}
    </span>
  );
}
