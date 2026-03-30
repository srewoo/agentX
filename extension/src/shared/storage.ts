import type { Signal, AppSettings } from "./types";

// ── Sensitive keys that must NEVER be synced to cloud ──
const SENSITIVE_KEYS = new Set([
  "llm_api_key", "openai_api_key", "gemini_api_key", "claude_api_key", "api_key",
]);

// chrome.storage.local — signals, last poll time, AND sensitive settings
export async function getStoredSignals(): Promise<Signal[]> {
  const result = await chrome.storage.local.get("signals");
  return (result.signals as Signal[]) || [];
}

export async function setStoredSignals(signals: Signal[]): Promise<void> {
  await chrome.storage.local.set({ signals });
}

export async function getLastPollTime(): Promise<string | null> {
  const result = await chrome.storage.local.get("lastPollTime");
  return (result.lastPollTime as string) || null;
}

export async function setLastPollTime(time: string): Promise<void> {
  await chrome.storage.local.set({ lastPollTime: time });
}

// ── Settings: split storage ──
// Non-sensitive settings → chrome.storage.sync (cross-device)
// API keys / secrets     → chrome.storage.local (never synced)

export async function getSettings(): Promise<Partial<AppSettings>> {
  const [syncResult, localResult] = await Promise.all([
    chrome.storage.sync.get("settings"),
    chrome.storage.local.get("sensitiveSettings"),
  ]);
  const syncSettings = (syncResult.settings as Partial<AppSettings>) || {};
  const localSecrets = (localResult.sensitiveSettings as Partial<AppSettings>) || {};
  return { ...syncSettings, ...localSecrets };
}

export async function saveSettings(settings: Partial<AppSettings>): Promise<void> {
  const syncPart: Record<string, unknown> = {};
  const localPart: Record<string, unknown> = {};

  for (const [key, value] of Object.entries(settings)) {
    if (SENSITIVE_KEYS.has(key)) {
      localPart[key] = value;
    } else {
      syncPart[key] = value;
    }
  }

  await Promise.all([
    chrome.storage.sync.set({ settings: syncPart }),
    chrome.storage.local.set({ sensitiveSettings: localPart }),
  ]);
}

export async function getBackendUrl(): Promise<string> {
  const settings = await getSettings();
  return (settings as Record<string, string>).backend_url || "http://localhost:8020";
}
