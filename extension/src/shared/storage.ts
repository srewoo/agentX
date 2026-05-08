import type { Signal, AppSettings } from "./types";

// ── Sensitive keys that must NEVER be synced to cloud ──
// Anything in this set is routed exclusively through chrome.storage.local.
// chrome.storage.sync is replicated to the user's Google account — secrets
// must never cross that boundary.
export const SENSITIVE_KEYS: ReadonlySet<string> = new Set([
  // LLM provider keys
  "llm_api_key",
  "openai_api_key",
  "gemini_api_key",
  "claude_api_key",
  "api_key",
  // Messaging / alerting
  "telegram_bot_token",
  "telegram_chat_id",
  "email_smtp_password",
  "twilio_auth_token",
  // Broker tokens
  "kite_access_token",
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
  // Read existing values so a partial save doesn't wipe untouched keys.
  const [existingSync, existingLocal] = await Promise.all([
    chrome.storage.sync.get("settings"),
    chrome.storage.local.get("sensitiveSettings"),
  ]);
  const syncPart: Record<string, unknown> = { ...(existingSync.settings as Record<string, unknown> | undefined ?? {}) };
  const localPart: Record<string, unknown> = { ...(existingLocal.sensitiveSettings as Record<string, unknown> | undefined ?? {}) };

  for (const [key, value] of Object.entries(settings)) {
    if (SENSITIVE_KEYS.has(key)) {
      localPart[key] = value;
      // Defensive: if a sensitive key ever leaked into sync, scrub it.
      if (key in syncPart) delete syncPart[key];
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

/**
 * One-time migration: any historical SENSITIVE_KEYS values that were saved into
 * chrome.storage.sync (older builds) are copied into local and removed from sync.
 *
 * Safe to call repeatedly — no-op when nothing sensitive is in sync.
 * Returns the list of keys migrated (empty if none).
 */
export async function migrateSensitiveFromSync(): Promise<string[]> {
  const syncResult = await chrome.storage.sync.get("settings");
  const syncSettings = (syncResult.settings as Record<string, unknown> | undefined) ?? {};
  const sensitiveInSync: Record<string, unknown> = {};
  for (const k of Object.keys(syncSettings)) {
    if (SENSITIVE_KEYS.has(k)) sensitiveInSync[k] = syncSettings[k];
  }
  const migratedKeys = Object.keys(sensitiveInSync);
  if (migratedKeys.length === 0) return [];

  const localResult = await chrome.storage.local.get("sensitiveSettings");
  const localSecrets = { ...((localResult.sensitiveSettings as Record<string, unknown> | undefined) ?? {}) };
  for (const [k, v] of Object.entries(sensitiveInSync)) {
    // Don't overwrite a value already present in local (treat local as authoritative).
    if (!(k in localSecrets) || localSecrets[k] === undefined || localSecrets[k] === "") {
      localSecrets[k] = v;
    }
    delete syncSettings[k];
  }

  await Promise.all([
    chrome.storage.local.set({ sensitiveSettings: localSecrets }),
    chrome.storage.sync.set({ settings: syncSettings }),
  ]);

  // Mask values in any debug logs.
  const masked = migratedKeys.map((k) => `${k}=***`).join(",");
  console.log(`[agentX storage] migrated ${migratedKeys.length} sensitive keys from sync → local (${masked})`);
  return migratedKeys;
}
