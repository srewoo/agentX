// SettingsPanel
// ---------------------------------------------------------------------------
// Tabs:
//   1. Account & Backend URL
//   2. API Keys
//   3. Notification Channels
//   4. LLM Spend Cap
//   5. Watchlist
//   6. Allowed Sites
//   7. Theme & About
//
// This component is presentation + light orchestration. All persistence flows
// through props (`onPatchSettings`) so tests / Storybook can drive it with
// in-memory state and the parent owns the canonical settings store.

import { useState, type ReactNode } from "react";
import { Toast, type ToastState } from "./_primitives";
import ApiKeyForm, {
  type ApiKeyProvider,
  type ApiKeyStatus,
  type ApiKeyFormValue,
} from "./ApiKeyForm";
import ChannelsForm, {
  type ChannelsConfig,
  type ChannelsFormPatch,
  type ChannelDelivery,
  type ChannelKind,
} from "./ChannelsForm";
import SpendCapForm, { type LlmUsageToday } from "./SpendCapForm";
import WatchlistEditor, { type WatchlistRow } from "./WatchlistEditor";
import SiteAllowlistEditor from "./SiteAllowlistEditor";
import ThemeToggle, { type ThemePreference } from "./ThemeToggle";
import BackendUrlForm from "./BackendUrlForm";

interface SearchResult {
  symbol: string;
  name: string;
  exchange: string;
}

export interface SettingsPanelData {
  backendUrl: string;
  apiKeyStatuses: Record<ApiKeyProvider, ApiKeyStatus>;
  channels: ChannelsConfig;
  channelHistory: ChannelDelivery[];
  usage: LlmUsageToday;
  fxRate: number; // 1 USD in INR
  watchlist: WatchlistRow[];
  allowedSites: string[];
  theme: ThemePreference;
  appVersion: string;
}

export interface SettingsPanelHandlers {
  saveBackendUrl: (url: string) => Promise<void>;
  testBackend: (url: string) => Promise<{ ok: boolean; message: string }>;
  saveApiKey: (value: ApiKeyFormValue) => Promise<void>;
  testApiKey: (
    provider: ApiKeyProvider,
  ) => Promise<{ ok: boolean; message: string }>;
  patchChannel: (patch: ChannelsFormPatch) => Promise<void>;
  sendChannelTest: (
    channel: ChannelKind,
  ) => Promise<{ ok: boolean; message: string }>;
  saveSpendCap: (capUsd: number) => Promise<void>;
  saveWatchlist: (next: WatchlistRow[]) => Promise<void>;
  saveAllowedSites: (next: string[]) => Promise<void>;
  saveTheme: (next: ThemePreference) => Promise<void>;
  searchSymbols: (q: string) => Promise<SearchResult[]>;
}

interface Props {
  data: SettingsPanelData;
  handlers: SettingsPanelHandlers;
}

type TabKey =
  | "account"
  | "keys"
  | "channels"
  | "spend"
  | "watchlist"
  | "sites"
  | "theme";

const TABS: Array<{ key: TabKey; label: string }> = [
  { key: "account", label: "Account" },
  { key: "keys", label: "API Keys" },
  { key: "channels", label: "Channels" },
  { key: "spend", label: "Spend Cap" },
  { key: "watchlist", label: "Watchlist" },
  { key: "sites", label: "Sites" },
  { key: "theme", label: "Theme" },
];

export default function SettingsPanel({ data, handlers }: Props) {
  const [tab, setTab] = useState<TabKey>("account");
  const [toast, setToast] = useState<ToastState | null>(null);

  // Wrap async handlers with success/error toasts so children can stay focused
  // on UI; we do this once at the boundary.
  const wrap = <Args extends unknown[], R>(
    fn: (...args: Args) => Promise<R>,
    successMsg?: string,
  ): ((...args: Args) => Promise<R>) => {
    return async (...args) => {
      try {
        const r = await fn(...args);
        if (successMsg) setToast({ kind: "success", message: successMsg });
        return r;
      } catch (e) {
        setToast({
          kind: "error",
          message: e instanceof Error ? e.message : "Something went wrong.",
        });
        throw e;
      }
    };
  };

  return (
    <section aria-label="Settings" className="flex flex-col gap-3">
      <Tabs tab={tab} onChange={setTab} />

      <Panel id="tabpanel-account" hidden={tab !== "account"}>
        <BackendUrlForm
          value={data.backendUrl}
          onSave={wrap(handlers.saveBackendUrl, "Backend URL saved.")}
          onTest={handlers.testBackend}
        />
      </Panel>

      <Panel id="tabpanel-keys" hidden={tab !== "keys"}>
        <ApiKeyForm
          statuses={data.apiKeyStatuses}
          onSave={wrap(handlers.saveApiKey, "API key saved.")}
          onTest={handlers.testApiKey}
        />
      </Panel>

      <Panel id="tabpanel-channels" hidden={tab !== "channels"}>
        <ChannelsForm
          config={data.channels}
          history={data.channelHistory}
          onUpdate={wrap(handlers.patchChannel, "Channel updated.")}
          onSendTest={handlers.sendChannelTest}
        />
      </Panel>

      <Panel id="tabpanel-spend" hidden={tab !== "spend"}>
        <SpendCapForm
          usage={data.usage}
          fxRate={data.fxRate}
          onSave={wrap(handlers.saveSpendCap, "Spend cap updated.")}
        />
      </Panel>

      <Panel id="tabpanel-watchlist" hidden={tab !== "watchlist"}>
        <WatchlistEditor
          items={data.watchlist}
          onChange={wrap(handlers.saveWatchlist)}
          searchSymbols={handlers.searchSymbols}
        />
      </Panel>

      <Panel id="tabpanel-sites" hidden={tab !== "sites"}>
        <SiteAllowlistEditor
          patterns={data.allowedSites}
          onChange={wrap(handlers.saveAllowedSites)}
        />
      </Panel>

      <Panel id="tabpanel-theme" hidden={tab !== "theme"}>
        <ThemeToggle
          value={data.theme}
          onChange={(next) => {
            void wrap(handlers.saveTheme)(next);
          }}
        />
        <div className="mt-3 rounded-md border border-slate-800 bg-slate-900/60 p-3 text-xs text-slate-400">
          <p>
            <strong className="text-slate-200">agentX</strong> · v{data.appVersion}
          </p>
          <p className="mt-1">
            For research and education only. Not investment advice.
          </p>
        </div>
      </Panel>

      <Toast toast={toast} onDismiss={() => setToast(null)} />
    </section>
  );
}

function Tabs({
  tab,
  onChange,
}: {
  tab: TabKey;
  onChange: (next: TabKey) => void;
}) {
  return (
    <div
      role="tablist"
      aria-label="Settings sections"
      className="flex gap-1 overflow-x-auto border-b border-slate-800"
    >
      {TABS.map((t) => {
        const selected = t.key === tab;
        return (
          <button
            key={t.key}
            role="tab"
            type="button"
            id={`tab-${t.key}`}
            aria-controls={`tabpanel-${t.key}`}
            aria-selected={selected}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(t.key)}
            onKeyDown={(e) => {
              const idx = TABS.findIndex((x) => x.key === tab);
              if (e.key === "ArrowRight") {
                e.preventDefault();
                onChange(TABS[(idx + 1) % TABS.length].key);
              } else if (e.key === "ArrowLeft") {
                e.preventDefault();
                onChange(TABS[(idx - 1 + TABS.length) % TABS.length].key);
              }
            }}
            className={[
              "px-3 py-1.5 text-xs font-medium whitespace-nowrap border-b-2",
              "focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400",
              selected
                ? "border-emerald-500 text-emerald-300"
                : "border-transparent text-slate-400 hover:text-slate-200",
            ].join(" ")}
          >
            {t.label}
          </button>
        );
      })}
    </div>
  );
}

function Panel({
  id,
  hidden,
  children,
}: {
  id: string;
  hidden: boolean;
  children: ReactNode;
}) {
  // Keep panels mounted but hidden so input state survives tab switches.
  return (
    <div
      role="tabpanel"
      id={id}
      aria-labelledby={id.replace("tabpanel-", "tab-")}
      hidden={hidden}
    >
      {hidden ? null : children}
    </div>
  );
}
