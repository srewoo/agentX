import { useState, useEffect, useCallback } from "react";
import type { AppSettings } from "../../shared/types";
import { getSettings, saveSettings } from "../../shared/storage";
import { api } from "../../shared/api";

const DEFAULTS: Partial<AppSettings> = {
  alert_interval_minutes: "30",
  risk_mode: "balanced",
  signal_types: ["intraday", "swing", "long_term"],
  llm_provider: "gemini",
  llm_model: "gemini-3.1-flash",
  // Advisor-mode defaults — Van Tharp 1% risk, ATR×1.5 stop, ATR×3 target (1:2 R:R),
  // 0.5% round-trip cost (typical retail discount-broker brokerage + STT + slippage),
  // and regime-aware filtering on by default.
  capital: 100000,
  risk_per_trade_pct: 1.0,
  atr_sl_mult: 1.5,
  atr_target_mult: 3.0,
  regime_filter: true,
  roundtrip_cost_pct: 0.5,
  dedupe_signals: true,
  auto_paper_trade: false,
  auto_paper_min_strength: 8,
  auto_paper_max_open: 10,
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
      // Use functional setState to avoid stale closure over `settings`
      let merged: Partial<AppSettings> = {};
      setSettings((prev) => {
        merged = { ...prev, ...updates };
        return merged;
      });
      await saveSettings(merged);
      await api.updateSettings(updates);
      // Notify service worker to reconfigure alarm if interval changed
      if (updates.alert_interval_minutes) {
        chrome.runtime.sendMessage({ type: "SETTINGS_CHANGED" }).catch(() => {});
      }
    } finally {
      setSaving(false);
    }
  }, []);

  return { settings, loading, saving, update };
}
