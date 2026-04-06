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
