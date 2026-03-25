/**
 * agentX Service Worker (Manifest V3)
 *
 * Key MV3 constraint: service workers are EPHEMERAL — they can be killed after
 * 30 seconds of inactivity. Use chrome.alarms for periodic work, never setInterval.
 */
import { getStoredSignals, setStoredSignals, getLastPollTime, setLastPollTime, getSettings } from "../shared/storage";
import type { Signal } from "../shared/types";

const ALARM_NAME = "stockpilot-scan";
const DEFAULT_INTERVAL_MINUTES = 30;
const MAX_SIGNALS = 100;

// ── Alarm setup ──────────────────────────────────────────────────────────────

async function registerAlarm(periodMinutes = DEFAULT_INTERVAL_MINUTES): Promise<void> {
  await chrome.alarms.clear(ALARM_NAME);
  chrome.alarms.create(ALARM_NAME, {
    delayInMinutes: 1, // First fire after 1 minute
    periodInMinutes: periodMinutes,
  });
}

async function getIntervalFromSettings(): Promise<number> {
  try {
    const settings = await getSettings();
    const interval = parseInt((settings as Record<string, string>).alert_interval_minutes || "30");
    return isNaN(interval) ? DEFAULT_INTERVAL_MINUTES : interval;
  } catch {
    return DEFAULT_INTERVAL_MINUTES;
  }
}

// ── Signal fetching ───────────────────────────────────────────────────────────

async function pollSignals(): Promise<void> {
  try {
    const settings = await getSettings() as Record<string, string>;
    const backendUrl = settings.backend_url || "http://localhost:8020";
    const apiKey = settings.api_key || "";
    const lastPoll = await getLastPollTime();

    const url = lastPoll
      ? `${backendUrl}/api/signals/latest?since=${encodeURIComponent(lastPoll)}&limit=50`
      : `${backendUrl}/api/signals/latest?limit=50`;

    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (apiKey) headers["X-API-Key"] = apiKey;

    const res = await fetch(url, { headers });
    if (!res.ok) return;

    const data = await res.json() as { signals: Signal[]; unread_count: number };
    const newSignals = data.signals || [];

    if (newSignals.length > 0) {
      // Merge with existing stored signals (newest first, cap at MAX_SIGNALS)
      const existing = await getStoredSignals();
      const existingIds = new Set(existing.map((s) => s.id));
      const trulyNew = newSignals.filter((s) => !existingIds.has(s.id));
      const merged = [
        ...trulyNew,
        ...existing,
      ].slice(0, MAX_SIGNALS);

      await setStoredSignals(merged);
      await setLastPollTime(new Date().toISOString());

      // Update badge with unread count
      const unreadCount = merged.filter((s) => !s.read && !s.dismissed).length;
      await updateBadge(unreadCount, newSignals);

      // Desktop notifications for truly new signals (max 3 per poll to avoid spam)
      const notifyBatch = trulyNew.slice(0, 3);
      for (const signal of notifyBatch) {
        const action = signal.direction === "bullish" ? "BUY" : signal.direction === "bearish" ? "SELL" : "WATCH";
        try {
          chrome.notifications.create(`signal-${signal.id}`, {
            type: "basic",
            iconUrl: "/assets/icon-128.png",
            title: `${action} ${signal.symbol}`,
            message: signal.reason,
            priority: 2,
          });
        } catch (notifErr) {
          console.error("[agentX SW] Notification error:", notifErr);
        }
      }

      // Notify popup/content scripts of new signals
      chrome.runtime.sendMessage({ type: "SIGNALS_UPDATED", count: unreadCount }).catch(() => {});
    }
  } catch (err) {
    console.error("[agentX SW] Poll failed:", err);
  }
}

async function updateBadge(unreadCount: number, newSignals: Signal[]): Promise<void> {
  if (unreadCount === 0) {
    await chrome.action.setBadgeText({ text: "" });
    return;
  }

  // Determine badge color: red for bearish, green for bullish, amber for neutral/mixed
  const hasNew = newSignals.length > 0;
  const latestDirection = hasNew ? newSignals[0].direction : "neutral";
  const color = latestDirection === "bearish" ? "#EF4444" : latestDirection === "bullish" ? "#10B981" : "#F59E0B";

  await chrome.action.setBadgeText({ text: String(unreadCount) });
  await chrome.action.setBadgeBackgroundColor({ color });
}

// ── Message handlers ──────────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  const handle = async () => {
    switch (message.type) {
      case "GET_SIGNALS": {
        const signals = await getStoredSignals();
        const unread = signals.filter((s) => !s.read && !s.dismissed).length;
        return { signals, unread_count: unread };
      }
      case "MARK_READ": {
        const signals = await getStoredSignals();
        const updated = signals.map((s) =>
          s.id === message.signalId ? { ...s, read: true } : s
        );
        await setStoredSignals(updated);
        const unread = updated.filter((s) => !s.read && !s.dismissed).length;
        await updateBadge(unread, []);
        return { ok: true };
      }
      case "MARK_ALL_READ": {
        const signals = await getStoredSignals();
        const updated = signals.map((s) => ({ ...s, read: true }));
        await setStoredSignals(updated);
        await chrome.action.setBadgeText({ text: "" });
        return { ok: true };
      }
      case "DISMISS_SIGNAL": {
        const signals = await getStoredSignals();
        const updated = signals.map((s) =>
          s.id === message.signalId ? { ...s, dismissed: true } : s
        );
        await setStoredSignals(updated);
        return { ok: true };
      }
      case "GET_UNREAD_COUNT": {
        const signals = await getStoredSignals();
        return { count: signals.filter((s) => !s.read && !s.dismissed).length };
      }
      case "SETTINGS_CHANGED": {
        const interval = await getIntervalFromSettings();
        await registerAlarm(interval);
        return { ok: true };
      }
      case "OPEN_POPUP": {
        // chrome.action.openPopup() available Chrome 127+ only
        if (chrome.action.openPopup) {
          await (chrome.action as unknown as { openPopup: () => Promise<void> }).openPopup();
        }
        return { ok: true };
      }
      default:
        return null;
    }
  };

  handle().then(sendResponse).catch((err) => sendResponse({ error: String(err) }));
  return true; // Keep channel open for async response
});

// ── Lifecycle ────────────────────────────────────────────────────────────────

chrome.runtime.onInstalled.addListener(async () => {
  console.log("[agentX SW] Installed");
  const interval = await getIntervalFromSettings();
  await registerAlarm(interval);
  // Run initial poll immediately
  await pollSignals();
});

chrome.runtime.onStartup.addListener(async () => {
  const interval = await getIntervalFromSettings();
  await registerAlarm(interval);
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === ALARM_NAME) {
    await pollSignals();
  }
});
