import { useState, useEffect, useCallback } from "react";
import type { AppSettings } from "../../shared/types";
import { getSettings, saveSettings } from "../../shared/storage";
import { api } from "../../shared/api";

const DEFAULTS: Partial<AppSettings> = {
  alert_interval_minutes: "30",
  risk_mode: "balanced",
  signal_types: ["intraday", "swing", "long_term"],
  llm_provider: "gemini",
  llm_model: "gemini-2.0-flash",
};

export function useSettings() {
  const [settings, setSettings] = useState<Partial<AppSettings>>(DEFAULTS);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const load = async () => {
      try {
        // Load from chrome.storage (local copy) first
        const local = await getSettings();

        // Then try to fetch from backend for authoritative values
        try {
          const backendResp = await api.getSettings();
          const merged = { ...local, ...backendResp.settings };
          setSettings(merged);
          await saveSettings(merged);
        } catch {
          // Backend unreachable — use local copy
          if (Object.keys(local).length > 0) setSettings(local);
        }
      } finally {
        setLoading(false);
      }
    };
    load();
  }, []);

  const update = useCallback(async (updates: Partial<AppSettings>) => {
    setSaving(true);
    try {
      const merged = { ...settings, ...updates };
      setSettings(merged);
      await saveSettings(merged);
      await api.updateSettings(updates);
      // Notify service worker to reconfigure alarm if interval changed
      if (updates.alert_interval_minutes) {
        chrome.runtime.sendMessage({ type: "SETTINGS_CHANGED" }).catch(() => {});
      }
    } finally {
      setSaving(false);
    }
  }, [settings]);

  return { settings, loading, saving, update };
}
