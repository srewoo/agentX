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

// ── Market hours ─────────────────────────────────────────────────────────────

/** Returns true if NSE/BSE is open: Mon–Fri, 9:15 AM – 3:30 PM IST. */
function isMarketOpen(): boolean {
  // Date.getTime() is UTC ms; add IST offset (UTC+5:30) to get IST wall-clock via UTC accessors
  const IST_OFFSET_MS = (5 * 60 + 30) * 60 * 1000;
  const ist = new Date(Date.now() + IST_OFFSET_MS);
  const day = ist.getUTCDay(); // 0=Sun, 6=Sat
  if (day === 0 || day === 6) return false;
  const minutes = ist.getUTCHours() * 60 + ist.getUTCMinutes();
  return minutes >= 9 * 60 + 15 && minutes <= 15 * 60 + 30;
}

// ── Alarm setup ──────────────────────────────────────────────────────────────

/**
 * Register the periodic alarm.
 * If periodMinutes === 0 (manual-only mode), the existing alarm is cleared
 * and no new alarm is created.
 */
async function registerAlarm(periodMinutes = DEFAULT_INTERVAL_MINUTES): Promise<void> {
  await chrome.alarms.clear(ALARM_NAME);
  if (periodMinutes === 0) return; // Manual-only: no alarm
  chrome.alarms.create(ALARM_NAME, {
    delayInMinutes: 1, // First fire after 1 minute
    periodInMinutes: periodMinutes,
  });
}

async function getIntervalFromSettings(): Promise<number> {
  try {
    const settings = await getSettings();
    const raw = (settings as Record<string, string>).alert_interval_minutes;
    // Handle both string ("30") and number (30) types safely
    const parsed = Number(raw);
    if (raw === undefined || raw === null || raw === "") return DEFAULT_INTERVAL_MINUTES;
    if (parsed === 0) return 0; // manual-only
    return isNaN(parsed) || parsed < 0 ? DEFAULT_INTERVAL_MINUTES : Math.round(parsed);
  } catch {
    return DEFAULT_INTERVAL_MINUTES;
  }
}

// ── Signal fetching ───────────────────────────────────────────────────────────

async function pollSignals(force = false): Promise<void> {
  // Skip auto-polls outside market hours; manual triggers (force=true) always run
  if (!force && !isMarketOpen()) {
    console.log("[agentX SW] Market closed — skipping auto-poll");
    return;
  }
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

    // 30s timeout to prevent hanging if backend is unresponsive
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30_000);
    let res: Response;
    try {
      res = await fetch(url, { headers, signal: controller.signal });
    } finally {
      clearTimeout(timer);
    }
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
        await registerAlarm(interval); // passes 0 for manual-only → clears alarm
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
