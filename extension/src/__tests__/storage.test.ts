import { describe, it, expect, beforeEach } from "vitest";
import { resetChromeStorage } from "./setup";
import {
  getStoredSignals,
  setStoredSignals,
  getLastPollTime,
  setLastPollTime,
  getSettings,
  saveSettings,
  getBackendUrl,
  SENSITIVE_KEYS,
  migrateSensitiveFromSync,
} from "../shared/storage";
import type { Signal } from "../shared/types";

function makeSignal(id: string): Signal {
  return {
    id,
    symbol: "TCS",
    signal_type: "price_spike",
    direction: "bullish",
    strength: 6,
    reason: "Test",
    risk: null,
    llm_summary: null,
    current_price: 3800,
    metadata: {},
    created_at: new Date().toISOString(),
    read: false,
    dismissed: false,
  };
}

describe("storage", () => {
  beforeEach(() => {
    resetChromeStorage();
  });

  describe("signals", () => {
    it("should return empty array when no signals stored", async () => {
      const signals = await getStoredSignals();
      expect(signals).toEqual([]);
    });

    it("should store and retrieve signals", async () => {
      const signals = [makeSignal("s1"), makeSignal("s2")];
      await setStoredSignals(signals);
      const retrieved = await getStoredSignals();
      expect(retrieved).toHaveLength(2);
      expect(retrieved[0].id).toBe("s1");
    });

    it("should overwrite existing signals", async () => {
      await setStoredSignals([makeSignal("old")]);
      await setStoredSignals([makeSignal("new")]);
      const retrieved = await getStoredSignals();
      expect(retrieved).toHaveLength(1);
      expect(retrieved[0].id).toBe("new");
    });
  });

  describe("lastPollTime", () => {
    it("should return null when not set", async () => {
      expect(await getLastPollTime()).toBeNull();
    });

    it("should store and retrieve poll time", async () => {
      const time = "2026-04-06T10:00:00Z";
      await setLastPollTime(time);
      expect(await getLastPollTime()).toBe(time);
    });
  });

  describe("settings", () => {
    it("should return empty object when no settings", async () => {
      const settings = await getSettings();
      expect(settings).toEqual({});
    });

    it("should split sensitive keys to local storage", async () => {
      await saveSettings({
        risk_mode: "aggressive",
        llm_api_key: "secret-key-123",
      });

      // Verify sensitive key went to local, non-sensitive to sync
      const localData = await chrome.storage.local.get("sensitiveSettings");
      const syncData = await chrome.storage.sync.get("settings");

      expect(localData.sensitiveSettings).toEqual({ llm_api_key: "secret-key-123" });
      expect(syncData.settings).toEqual({ risk_mode: "aggressive" });
    });

    it("should merge local and sync when reading", async () => {
      await saveSettings({
        risk_mode: "conservative",
        openai_api_key: "sk-test",
        alert_interval_minutes: "15",
      });

      const settings = await getSettings();
      expect(settings.risk_mode).toBe("conservative");
      expect(settings.openai_api_key).toBe("sk-test");
      expect(settings.alert_interval_minutes).toBe("15");
    });

    it("should isolate all sensitive keys", async () => {
      await saveSettings({
        llm_api_key: "k1",
        openai_api_key: "k2",
        gemini_api_key: "k3",
        claude_api_key: "k4",
        api_key: "k5",
      } as Record<string, string>);

      // All should be in local only
      const syncData = await chrome.storage.sync.get("settings");
      expect(syncData.settings).toEqual({});

      const localData = await chrome.storage.local.get("sensitiveSettings");
      expect(Object.keys(localData.sensitiveSettings || {})).toHaveLength(5);
    });
  });

  describe("SENSITIVE_KEYS routing", () => {
    it("should include all alerting and broker secrets", () => {
      const required = [
        "llm_api_key",
        "openai_api_key",
        "gemini_api_key",
        "claude_api_key",
        "api_key",
        "telegram_bot_token",
        "telegram_chat_id",
        "email_smtp_password",
        "twilio_auth_token",
        "kite_access_token",
      ];
      for (const k of required) {
        expect(SENSITIVE_KEYS.has(k)).toBe(true);
      }
    });

    it("should route telegram_bot_token + telegram_chat_id to local, never sync", async () => {
      await saveSettings({
        risk_mode: "balanced",
        telegram_bot_token: "12345:abcdef",
        telegram_chat_id: "-100987",
      } as Record<string, string>);

      const syncData = await chrome.storage.sync.get("settings");
      const localData = await chrome.storage.local.get("sensitiveSettings");

      expect((syncData.settings as Record<string, unknown>).telegram_bot_token).toBeUndefined();
      expect((syncData.settings as Record<string, unknown>).telegram_chat_id).toBeUndefined();
      expect((syncData.settings as Record<string, unknown>).risk_mode).toBe("balanced");

      expect((localData.sensitiveSettings as Record<string, unknown>).telegram_bot_token).toBe("12345:abcdef");
      expect((localData.sensitiveSettings as Record<string, unknown>).telegram_chat_id).toBe("-100987");
    });

    it("should route kite_access_token + email_smtp_password + twilio_auth_token to local", async () => {
      await saveSettings({
        kite_access_token: "kite-tok-xyz",
        email_smtp_password: "smtp-pw-123",
        twilio_auth_token: "twilio-auth-456",
      } as Record<string, string>);

      const syncData = await chrome.storage.sync.get("settings");
      const localData = await chrome.storage.local.get("sensitiveSettings");

      expect(syncData.settings).toEqual({});
      const local = localData.sensitiveSettings as Record<string, unknown>;
      expect(local.kite_access_token).toBe("kite-tok-xyz");
      expect(local.email_smtp_password).toBe("smtp-pw-123");
      expect(local.twilio_auth_token).toBe("twilio-auth-456");
    });

    it("should preserve existing settings on partial save", async () => {
      await saveSettings({ risk_mode: "balanced", openai_api_key: "k1" } as Record<string, string>);
      await saveSettings({ alert_interval_minutes: "30" } as Record<string, string>);

      const merged = await getSettings();
      expect(merged.risk_mode).toBe("balanced");
      expect((merged as Record<string, string>).openai_api_key).toBe("k1");
      expect((merged as Record<string, string>).alert_interval_minutes).toBe("30");
    });
  });

  describe("migrateSensitiveFromSync", () => {
    it("should be a no-op when no sensitive keys are in sync", async () => {
      await chrome.storage.sync.set({ settings: { risk_mode: "balanced" } });
      const migrated = await migrateSensitiveFromSync();
      expect(migrated).toEqual([]);
      const sync = await chrome.storage.sync.get("settings");
      expect(sync.settings).toEqual({ risk_mode: "balanced" });
    });

    it("should move sensitive keys from sync into local and remove them from sync", async () => {
      // Simulate a legacy install where secrets leaked into sync.
      await chrome.storage.sync.set({
        settings: {
          risk_mode: "balanced",
          telegram_bot_token: "leaked-token",
          telegram_chat_id: "leaked-chat",
          openai_api_key: "leaked-openai",
        },
      });

      const migrated = await migrateSensitiveFromSync();
      expect(migrated.sort()).toEqual(["openai_api_key", "telegram_bot_token", "telegram_chat_id"]);

      const sync = await chrome.storage.sync.get("settings");
      expect(sync.settings).toEqual({ risk_mode: "balanced" });

      const local = await chrome.storage.local.get("sensitiveSettings");
      const ls = local.sensitiveSettings as Record<string, unknown>;
      expect(ls.telegram_bot_token).toBe("leaked-token");
      expect(ls.telegram_chat_id).toBe("leaked-chat");
      expect(ls.openai_api_key).toBe("leaked-openai");
    });

    it("should not overwrite a value already present in local", async () => {
      await chrome.storage.local.set({
        sensitiveSettings: { telegram_bot_token: "current-local-token" },
      });
      await chrome.storage.sync.set({
        settings: { telegram_bot_token: "stale-sync-token", risk_mode: "balanced" },
      });

      await migrateSensitiveFromSync();

      const local = await chrome.storage.local.get("sensitiveSettings");
      expect((local.sensitiveSettings as Record<string, unknown>).telegram_bot_token)
        .toBe("current-local-token");

      const sync = await chrome.storage.sync.get("settings");
      // Stale sync value still scrubbed.
      expect((sync.settings as Record<string, unknown>).telegram_bot_token).toBeUndefined();
    });

    it("should be idempotent on repeat invocations", async () => {
      await chrome.storage.sync.set({
        settings: { telegram_bot_token: "tok" },
      });
      const first = await migrateSensitiveFromSync();
      const second = await migrateSensitiveFromSync();
      expect(first).toEqual(["telegram_bot_token"]);
      expect(second).toEqual([]);
    });
  });

  describe("backendUrl", () => {
    it("should return default when no setting", async () => {
      expect(await getBackendUrl()).toBe("http://localhost:8020");
    });

    it("should return custom URL when set", async () => {
      await saveSettings({ backend_url: "http://myserver:9000" } as Record<string, string>);
      expect(await getBackendUrl()).toBe("http://myserver:9000");
    });
  });
});
