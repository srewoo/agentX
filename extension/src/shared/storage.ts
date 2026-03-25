import type { Signal, AppSettings } from "./types";

// chrome.storage.local — signals, last poll time
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

// chrome.storage.sync — settings (synced across devices)
export async function getSettings(): Promise<Partial<AppSettings>> {
  const result = await chrome.storage.sync.get("settings");
  return (result.settings as Partial<AppSettings>) || {};
}

export async function saveSettings(settings: Partial<AppSettings>): Promise<void> {
  await chrome.storage.sync.set({ settings });
}

export async function getBackendUrl(): Promise<string> {
  const settings = await getSettings();
  return (settings as Record<string, string>).backend_url || "http://localhost:8020";
}
