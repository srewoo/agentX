// ChannelsForm
// ---------------------------------------------------------------------------
// Notification channel cards: Telegram, Email, WhatsApp, SMS.
// Each card has:
//   • per-channel enable toggle
//   • config inputs (write-only for secrets, regular for non-secret)
//   • "Send test" button that calls /api/alerts/test via the parent
//   • a delivery-history mini-list (most recent N)
// Secrets follow the same "never reflect" invariant as ApiKeyForm.

import { useId, useState } from "react";
import { Button, Field, Switch, TextInput } from "./_primitives";
import { isValidEmail, isValidIndianPhone, savedKeyHint } from "./_utils";

export type ChannelKind = "telegram" | "email" | "whatsapp" | "sms";

export interface ChannelDelivery {
  channel: ChannelKind;
  status: "ok" | "failed";
  message?: string;
  at: string; // ISO
}

export interface ChannelsConfig {
  telegram: {
    enabled: boolean;
    chatId: string;
    botTokenConfigured: boolean;
    botTokenCharCount?: number | null;
  };
  email: {
    enabled: boolean;
    address: string;
  };
  whatsapp: {
    enabled: boolean;
    // +91 default
    phone: string;
  };
  sms: {
    enabled: boolean;
    phone: string;
  };
}

export interface ChannelsFormPatch {
  channel: ChannelKind;
  enabled?: boolean;
  // For telegram, supplying a non-empty `secret` overwrites the bot token.
  secret?: string;
  // Non-secret config (chatId, address, phone).
  config?: Record<string, string>;
}

interface Props {
  config: ChannelsConfig;
  history: ChannelDelivery[];
  onUpdate: (patch: ChannelsFormPatch) => Promise<void>;
  onSendTest: (channel: ChannelKind) => Promise<{ ok: boolean; message: string }>;
}

export default function ChannelsForm({
  config,
  history,
  onUpdate,
  onSendTest,
}: Props) {
  return (
    <div className="flex flex-col gap-3">
      <TelegramCard
        value={config.telegram}
        history={history.filter((h) => h.channel === "telegram").slice(0, 3)}
        onUpdate={onUpdate}
        onSendTest={onSendTest}
      />
      <EmailCard
        value={config.email}
        history={history.filter((h) => h.channel === "email").slice(0, 3)}
        onUpdate={onUpdate}
        onSendTest={onSendTest}
      />
      <PhoneCard
        kind="whatsapp"
        title="WhatsApp"
        value={config.whatsapp}
        history={history.filter((h) => h.channel === "whatsapp").slice(0, 3)}
        onUpdate={onUpdate}
        onSendTest={onSendTest}
      />
      <PhoneCard
        kind="sms"
        title="SMS"
        value={config.sms}
        history={history.filter((h) => h.channel === "sms").slice(0, 3)}
        onUpdate={onUpdate}
        onSendTest={onSendTest}
      />
    </div>
  );
}

// ── Card shell ────────────────────────────────────────────────────────────

function CardShell({
  title,
  enabled,
  onToggle,
  history,
  onSendTest,
  testDisabled,
  testDisabledReason,
  children,
}: {
  title: string;
  enabled: boolean;
  onToggle: (next: boolean) => void;
  history: ChannelDelivery[];
  onSendTest: () => Promise<void>;
  testDisabled: boolean;
  testDisabledReason?: string;
  children: React.ReactNode;
}) {
  const [testing, setTesting] = useState(false);
  return (
    <section
      className="rounded-md border border-slate-800 bg-slate-900/60 p-3"
      aria-label={`${title} channel`}
    >
      <header className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-medium text-slate-100">{title}</h3>
        <Switch
          label={`Enable ${title} notifications`}
          checked={enabled}
          onChange={onToggle}
        />
      </header>

      <div className={enabled ? "" : "opacity-60 pointer-events-none"}>
        {children}
      </div>

      <div className="mt-3 flex items-center justify-between">
        <Button
          variant="secondary"
          loading={testing}
          disabled={!enabled || testDisabled}
          title={testDisabledReason}
          onClick={async () => {
            setTesting(true);
            try {
              await onSendTest();
            } finally {
              setTesting(false);
            }
          }}
        >
          Send test
        </Button>
        <span className="text-xs text-slate-500">
          {history.length === 0 ? "No deliveries yet" : "Recent:"}
        </span>
      </div>

      {history.length > 0 ? (
        <ul className="mt-2 space-y-1 text-xs" aria-label={`${title} delivery history`}>
          {history.map((h, i) => (
            <li key={`${h.at}-${i}`} className="flex items-center justify-between">
              <span
                className={
                  h.status === "ok" ? "text-emerald-400" : "text-rose-400"
                }
                aria-label={h.status === "ok" ? "Delivered" : "Failed"}
              >
                {h.status === "ok" ? "✓" : "✗"}{" "}
                <span className="text-slate-400">
                  {new Date(h.at).toLocaleString()}
                </span>
              </span>
              {h.message ? (
                <span className="text-slate-500 truncate ml-2 max-w-[60%]">
                  {h.message}
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

// ── Telegram ──────────────────────────────────────────────────────────────

function TelegramCard({
  value,
  history,
  onUpdate,
  onSendTest,
}: {
  value: ChannelsConfig["telegram"];
  history: ChannelDelivery[];
  onUpdate: Props["onUpdate"];
  onSendTest: Props["onSendTest"];
}) {
  const tokenId = useId();
  const chatId = useId();
  const [chat, setChat] = useState(value.chatId);
  const [token, setToken] = useState("");
  const [reveal, setReveal] = useState(false);
  const [chatErr, setChatErr] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{ ok: boolean; message: string } | null>(
    null,
  );

  const dirty = chat !== value.chatId || token.length > 0;

  const handleSave = async () => {
    setChatErr(null);
    if (!chat.trim()) {
      setChatErr("Chat ID is required.");
      return;
    }
    await onUpdate({
      channel: "telegram",
      config: { chatId: chat.trim() },
      ...(token ? { secret: token.trim() } : {}),
    });
    setToken("");
    setReveal(false);
  };

  return (
    <CardShell
      title="Telegram"
      enabled={value.enabled}
      onToggle={(next) => onUpdate({ channel: "telegram", enabled: next })}
      history={history}
      testDisabled={!value.botTokenConfigured || !value.chatId}
      testDisabledReason={
        !value.botTokenConfigured
          ? "Configure bot token first"
          : !value.chatId
            ? "Set chat ID first"
            : undefined
      }
      onSendTest={async () => {
        const r = await onSendTest("telegram");
        setFeedback(r);
      }}
    >
      <div className="flex flex-col gap-3">
        <Field
          label="Bot token"
          htmlFor={tokenId}
          hint={
            value.botTokenConfigured
              ? `${savedKeyHint(value.botTokenCharCount)} — type to overwrite`
              : "Paste your BotFather token"
          }
        >
          <div className="flex gap-2">
            <TextInput
              id={tokenId}
              value={token}
              onChange={(e) => setToken(e.target.value)}
              type={reveal ? "text" : "password"}
              placeholder={
                value.botTokenConfigured
                  ? "Configured ✓ — type to overwrite"
                  : "123456:ABC-DEF..."
              }
              autoComplete="off"
              spellCheck={false}
              data-1p-ignore
              data-lpignore="true"
            />
            <Button
              variant="ghost"
              onClick={() => setReveal((r) => !r)}
              disabled={token.length === 0}
              aria-pressed={reveal}
              aria-label={reveal ? "Hide entered token" : "Show entered token"}
            >
              {reveal ? "Hide" : "Show"}
            </Button>
          </div>
        </Field>

        <Field
          label="Chat ID"
          htmlFor={chatId}
          error={chatErr}
          hint={!chatErr ? "Numeric ID, or @channelname" : undefined}
        >
          <TextInput
            id={chatId}
            value={chat}
            onChange={(e) => setChat(e.target.value)}
            placeholder="-100123456789 or @mychannel"
            invalid={Boolean(chatErr)}
          />
        </Field>

        <div className="flex items-center justify-between">
          <Button variant="primary" onClick={handleSave} disabled={!dirty}>
            Save
          </Button>
          {feedback ? (
            <span
              role="status"
              className={`text-xs ${
                feedback.ok ? "text-emerald-400" : "text-rose-400"
              }`}
            >
              {feedback.ok ? "✓ " : "✗ "}
              {feedback.message}
            </span>
          ) : null}
        </div>
      </div>
    </CardShell>
  );
}

// ── Email ─────────────────────────────────────────────────────────────────

function EmailCard({
  value,
  history,
  onUpdate,
  onSendTest,
}: {
  value: ChannelsConfig["email"];
  history: ChannelDelivery[];
  onUpdate: Props["onUpdate"];
  onSendTest: Props["onSendTest"];
}) {
  const id = useId();
  const [addr, setAddr] = useState(value.address);
  const [err, setErr] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{ ok: boolean; message: string } | null>(
    null,
  );

  const handleSave = async () => {
    if (!isValidEmail(addr)) {
      setErr("Enter a valid email address.");
      return;
    }
    setErr(null);
    await onUpdate({
      channel: "email",
      config: { address: addr.trim() },
    });
  };

  return (
    <CardShell
      title="Email"
      enabled={value.enabled}
      onToggle={(next) => onUpdate({ channel: "email", enabled: next })}
      history={history}
      testDisabled={!isValidEmail(value.address)}
      testDisabledReason={
        !isValidEmail(value.address) ? "Save a valid address first" : undefined
      }
      onSendTest={async () => {
        const r = await onSendTest("email");
        setFeedback(r);
      }}
    >
      <Field label="Email address" htmlFor={id} error={err}>
        <TextInput
          id={id}
          type="email"
          value={addr}
          onChange={(e) => setAddr(e.target.value)}
          placeholder="you@example.com"
          autoComplete="email"
          invalid={Boolean(err)}
        />
      </Field>
      <div className="mt-3 flex items-center justify-between">
        <Button
          variant="primary"
          onClick={handleSave}
          disabled={addr === value.address}
        >
          Save
        </Button>
        {feedback ? (
          <span
            role="status"
            className={`text-xs ${
              feedback.ok ? "text-emerald-400" : "text-rose-400"
            }`}
          >
            {feedback.ok ? "✓ " : "✗ "}
            {feedback.message}
          </span>
        ) : null}
      </div>
    </CardShell>
  );
}

// ── Phone (WhatsApp + SMS) ────────────────────────────────────────────────

function PhoneCard({
  kind,
  title,
  value,
  history,
  onUpdate,
  onSendTest,
}: {
  kind: "whatsapp" | "sms";
  title: string;
  value: { enabled: boolean; phone: string };
  history: ChannelDelivery[];
  onUpdate: Props["onUpdate"];
  onSendTest: Props["onSendTest"];
}) {
  const id = useId();
  // Strip leading +91 / 91 for editing convenience; always store as +91XXXXXXXXXX.
  const stripDefault = (v: string) =>
    v.replace(/^\+?91/, "").replace(/\D/g, "");
  const [local, setLocal] = useState(stripDefault(value.phone));
  const [err, setErr] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{ ok: boolean; message: string } | null>(
    null,
  );

  const fullPhone = local ? `+91${local}` : "";

  const handleSave = async () => {
    if (!isValidIndianPhone(fullPhone)) {
      setErr("Enter a 10-digit Indian phone number.");
      return;
    }
    setErr(null);
    await onUpdate({
      channel: kind,
      config: { phone: fullPhone },
    });
  };

  return (
    <CardShell
      title={title}
      enabled={value.enabled}
      onToggle={(next) => onUpdate({ channel: kind, enabled: next })}
      history={history}
      testDisabled={!isValidIndianPhone(value.phone)}
      testDisabledReason={
        !isValidIndianPhone(value.phone) ? "Save a valid number first" : undefined
      }
      onSendTest={async () => {
        const r = await onSendTest(kind);
        setFeedback(r);
      }}
    >
      <Field
        label={`${title} phone number`}
        htmlFor={id}
        error={err}
        hint={!err ? "10-digit mobile (India)" : undefined}
      >
        <div className="flex gap-2 items-center">
          <span
            aria-hidden="true"
            className="px-2 py-1.5 rounded-md bg-slate-800 text-slate-300 text-sm"
          >
            +91
          </span>
          <TextInput
            id={id}
            type="tel"
            inputMode="numeric"
            value={local}
            onChange={(e) =>
              setLocal(e.target.value.replace(/\D/g, "").slice(0, 10))
            }
            placeholder="9876543210"
            autoComplete="tel-national"
            invalid={Boolean(err)}
          />
        </div>
      </Field>
      <div className="mt-3 flex items-center justify-between">
        <Button
          variant="primary"
          onClick={handleSave}
          disabled={fullPhone === value.phone}
        >
          Save
        </Button>
        {feedback ? (
          <span
            role="status"
            className={`text-xs ${
              feedback.ok ? "text-emerald-400" : "text-rose-400"
            }`}
          >
            {feedback.ok ? "✓ " : "✗ "}
            {feedback.message}
          </span>
        ) : null}
      </div>
    </CardShell>
  );
}
