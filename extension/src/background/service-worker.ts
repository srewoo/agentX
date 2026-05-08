/**
 * agentX Service Worker (Manifest V3)
 *
 * Key MV3 constraint: service workers are EPHEMERAL — they can be killed after
 * 30 seconds of inactivity. Use chrome.alarms for periodic work, never setInterval.
 */
import { getStoredSignals, setStoredSignals, getLastPollTime, setLastPollTime, getSettings, getBackendUrl, migrateSensitiveFromSync } from "../shared/storage";
import type { Signal, PaperTrade, AppSettings } from "../shared/types";

const ALARM_NAME = "stockpilot-scan";
const PAPER_ALARM_NAME = "stockpilot-paper-track";
const DEFAULT_INTERVAL_MINUTES = 30;
const PAPER_INTERVAL_MINUTES = 5; // SL/Target check cadence during market hours
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

/** Paper-trade tracker alarm — runs whether or not the user has the popup open. */
async function registerPaperAlarm(): Promise<void> {
  await chrome.alarms.clear(PAPER_ALARM_NAME);
  chrome.alarms.create(PAPER_ALARM_NAME, {
    delayInMinutes: 1,
    periodInMinutes: PAPER_INTERVAL_MINUTES,
  });
}

// ── Auto-paper-trade from incoming signals ───────────────────────────────────
//
// Closes the agent loop: high-conviction signals get auto-entered into the
// paper book with ATR-based SL/Target and Van-Tharp position sizing. The
// auto-tracker (below) then closes them when SL/Target is hit, building a
// self-improving performance dataset without any user intervention.
//
// Opt-in via Settings.auto_paper_trade. Strength threshold and capital risk
// are read live each cycle so the user can tune from the UI without restart.

async function autoPaperFromSignals(newSignals: Signal[]): Promise<void> {
  if (!newSignals.length) return;
  const settings = (await getSettings()) as Partial<AppSettings>;
  if (!settings.auto_paper_trade) return;

  const minStrength = Number(settings.auto_paper_min_strength ?? 8);
  const capital = Number(settings.capital ?? 100000);
  const riskPct = Number(settings.risk_per_trade_pct ?? 1.0) / 100;
  const slMult = Number(settings.atr_sl_mult ?? 1.5);
  const tgtMult = Number(settings.atr_target_mult ?? 3.0);
  const maxOpen = Number(settings.auto_paper_max_open ?? 10);
  const mutedSyms = new Set(settings.muted_symbols ?? []);
  const mutedTypes = new Set(settings.muted_signal_types ?? []);

  // Filter to actionable signals
  const candidates = newSignals.filter((s) => {
    if (s.direction === "neutral") return false;
    if (s.strength < minStrength) return false;
    if (s.current_price == null || s.current_price <= 0) return false;
    if (mutedSyms.has(s.symbol.toUpperCase())) return false;
    if (mutedTypes.has(s.signal_type)) return false;
    return true;
  });
  if (!candidates.length) return;

  // Cap total open positions (portfolio heat protection)
  const r = await chrome.storage.local.get("paperTrades");
  const trades = (r.paperTrades as PaperTrade[] | undefined) ?? [];
  const openCount = trades.filter((t) => t.status === "open").length;
  let remainingSlots = Math.max(0, maxOpen - openCount);
  if (remainingSlots === 0) {
    console.log("[agentX SW] auto-paper: max open reached, skipping");
    return;
  }

  // Skip duplicates — never auto-take a second trade on a symbol already open
  const openSyms = new Set(trades.filter((t) => t.status === "open").map((t) => t.symbol.toUpperCase()));
  const seenSignalIds = new Set(trades.map((t) => t.signal_id).filter(Boolean));

  const newTrades: PaperTrade[] = [];
  for (const s of candidates) {
    if (remainingSlots <= 0) break;
    if (openSyms.has(s.symbol.toUpperCase())) continue;
    if (seenSignalIds.has(s.id)) continue;

    // Risk plan — prefer ATR from signal metadata; fall back to 2%-band heuristic
    const price = s.current_price!;
    const atrMeta = typeof s.metadata?.atr === "number" ? (s.metadata.atr as number) : null;
    const riskPerShare = atrMeta != null && atrMeta > 0 ? atrMeta * slMult : price * 0.02 * slMult;
    const rewardPerShare = atrMeta != null && atrMeta > 0 ? atrMeta * tgtMult : price * 0.02 * tgtMult;
    const dir = s.direction === "bullish" ? 1 : -1;
    const stop = price - dir * riskPerShare;
    const target = price + dir * rewardPerShare;
    const qty = Math.max(1, Math.floor((capital * riskPct) / riskPerShare));

    newTrades.push({
      id: crypto.randomUUID(),
      symbol: s.symbol,
      side: s.direction === "bearish" ? "SELL" : "BUY",
      qty,
      entry_price: price,
      entry_at: new Date().toISOString(),
      signal_id: s.id,
      target,
      stop_loss: stop,
      status: "open",
      notes: `auto-entry · ${s.signal_type} · str ${s.strength}`,
    });
    remainingSlots--;
    openSyms.add(s.symbol.toUpperCase());
  }

  if (!newTrades.length) return;
  await chrome.storage.local.set({ paperTrades: [...newTrades, ...trades] });
  console.log(`[agentX SW] auto-paper: opened ${newTrades.length} positions`);

  // One consolidated notification (Chrome rate-limits per-id notifications)
  try {
    const summary = newTrades.slice(0, 3).map((t) => `${t.side} ${t.symbol} qty ${t.qty}`).join(" · ");
    chrome.notifications.create(`paper-auto-${Date.now()}`, {
      type: "basic",
      iconUrl: "/assets/icon-128.png",
      title: `📒 Auto-paper: ${newTrades.length} new position${newTrades.length > 1 ? "s" : ""}`,
      message: summary + (newTrades.length > 3 ? ` +${newTrades.length - 3} more` : ""),
      priority: 1,
    });
  } catch { /* notification errors are non-fatal */ }
}

// ── Paper-trade auto-tracking ────────────────────────────────────────────────

interface QuoteResp { symbol: string; price: number | null; }

async function fetchQuote(baseUrl: string, symbol: string, apiKey: string): Promise<number | null> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (apiKey) headers["X-API-Key"] = apiKey;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 8_000);
  try {
    const res = await fetch(`${baseUrl}/api/stocks/${encodeURIComponent(symbol)}/quote`, { headers, signal: ctrl.signal });
    if (!res.ok) return null;
    const j = await res.json() as QuoteResp;
    return j.price;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Walk every open paper trade. If price has crossed SL or Target, close the
 * trade with the reason and notify the user. Reads/writes paper trades via
 * chrome.storage.local directly so this works whether or not the popup is open.
 *
 * Triggering rules:
 *  - BUY  trade: target hit when price ≥ target; stop hit when price ≤ stop_loss.
 *  - SELL trade: target hit when price ≤ target; stop hit when price ≥ stop_loss.
 * Trades without target/stop are skipped (manual-only management).
 */
async function trackPaperTrades(): Promise<void> {
  if (!isMarketOpen()) return;
  try {
    const r = await chrome.storage.local.get("paperTrades");
    const trades = (r.paperTrades as PaperTrade[] | undefined) ?? [];
    const open = trades.filter((t) => t.status === "open" && (t.target != null || t.stop_loss != null));
    if (open.length === 0) return;

    const baseUrl = await getBackendUrl();
    const settings = await getSettings() as Record<string, string>;
    const apiKey = settings.api_key || "";

    // Unique symbols only — coalesce multiple trades on the same ticker
    const uniqueSymbols = Array.from(new Set(open.map((t) => t.symbol)));
    const priceBySym = new Map<string, number>();

    // Concurrency-cap fan-out
    const inflight: Promise<void>[] = [];
    const cap = 4;
    let cursor = 0;
    while (cursor < uniqueSymbols.length || inflight.length > 0) {
      while (inflight.length < cap && cursor < uniqueSymbols.length) {
        const sym = uniqueSymbols[cursor++];
        inflight.push(fetchQuote(baseUrl, sym, apiKey).then((p) => {
          if (p != null) priceBySym.set(sym, p);
        }));
      }
      if (inflight.length === 0) break;
      const settled = await Promise.race(inflight.map((p, i) => p.then(() => i)));
      inflight.splice(settled, 1);
    }

    // Apply SL/Target rules
    let closedAny = false;
    const updated: PaperTrade[] = trades.map((t) => {
      if (t.status !== "open") return t;
      const price = priceBySym.get(t.symbol);
      if (price == null) return t;
      const dir = t.side === "BUY" ? 1 : -1;
      const targetHit = t.target != null && (dir === 1 ? price >= t.target : price <= t.target);
      const stopHit = t.stop_loss != null && (dir === 1 ? price <= t.stop_loss : price >= t.stop_loss);
      if (!targetHit && !stopHit) return t;

      const reason = targetHit ? "target" : "stop";
      const exit_price = targetHit ? t.target! : t.stop_loss!;
      closedAny = true;

      // User-visible notification
      try {
        const pnlPct = ((exit_price - t.entry_price) / t.entry_price) * 100 * dir;
        const verb = reason === "target" ? "🎯 Target hit" : "🛑 Stop hit";
        chrome.notifications.create(`paper-close-${t.id}`, {
          type: "basic",
          iconUrl: "/assets/icon-128.png",
          title: `${verb} · ${t.symbol}`,
          message: `${t.side} closed @ ₹${exit_price.toFixed(1)} · ${pnlPct >= 0 ? "+" : ""}${pnlPct.toFixed(2)}% · qty ${t.qty}`,
          priority: 2,
        });
      } catch { /* ignore notification errors */ }

      return {
        ...t,
        status: "closed" as const,
        exit_price,
        exit_at: new Date().toISOString(),
        notes: t.notes ? `${t.notes} · auto-${reason}` : `auto-${reason}`,
      };
    });

    if (closedAny) {
      await chrome.storage.local.set({ paperTrades: updated });
    }
  } catch (err) {
    console.warn("[agentX SW] paper tracker error:", err);
  }
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

// ── Telegram forwarding ──────────────────────────────────────────────────────
//
// Sensitive values (telegram_bot_token, telegram_chat_id) are sourced from
// getSettings() — which routes SENSITIVE_KEYS through chrome.storage.local
// only. They MUST NEVER be read directly from chrome.storage.sync.

/** Mask a secret for safe debug logging — keeps just enough to identify which key is set. */
function maskSecret(s: string): string {
  if (!s) return "";
  if (s.length <= 6) return "***";
  return `${s.slice(0, 3)}***${s.slice(-2)}`;
}

async function forwardSignalsToTelegram(trulyNew: Signal[]): Promise<void> {
  const settingsAll = (await getSettings()) as Record<string, string | number | undefined>;
  const tgToken = String(settingsAll.telegram_bot_token || "");
  const tgChat = String(settingsAll.telegram_chat_id || "");
  const minStrength = Number(settingsAll.telegram_min_strength ?? 8);
  if (!tgToken || !tgChat) return;

  const eligible = trulyNew.filter((s) => s.strength >= minStrength);
  if (eligible.length === 0) return;

  const baseUrl = await getBackendUrl();
  const apiKey = String(settingsAll.api_key || "");

  // Try backend delivery first (keeps secrets server-side, allows audit logging).
  let backendOk = false;
  try {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (apiKey) headers["X-API-Key"] = apiKey;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 5_000);
    try {
      const res = await fetch(`${baseUrl}/api/alerts/test`, {
        method: "POST",
        headers,
        body: JSON.stringify({
          channel: "telegram",
          signals: eligible.map((s) => ({
            id: s.id,
            symbol: s.symbol,
            direction: s.direction,
            strength: s.strength,
            reason: s.reason,
            current_price: s.current_price,
          })),
        }),
        signal: ctrl.signal,
      });
      backendOk = res.ok;
    } finally {
      clearTimeout(timer);
    }
  } catch {
    backendOk = false;
  }

  if (backendOk) {
    console.log(`[agentX SW] Telegram via backend ok · token=${maskSecret(tgToken)} · n=${eligible.length}`);
    return;
  }

  // Backend unreachable — fall back to direct Telegram call.
  console.warn(`[agentX SW] backend unreachable, direct Telegram fallback · token=${maskSecret(tgToken)}`);
  for (const sig of eligible) {
    const action = sig.direction === "bullish" ? "BUY" : sig.direction === "bearish" ? "SELL" : "WATCH";
    const text = `*agentX* ${action} ${sig.symbol} (${sig.strength}/10)\n${sig.reason}${sig.current_price ? `\nPrice: ₹${sig.current_price}` : ""}`;
    fetch(`https://api.telegram.org/bot${encodeURIComponent(tgToken)}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: tgChat, text, parse_mode: "Markdown" }),
    }).catch(() => {});
  }
}

// ── Custom-domain runtime injection ──────────────────────────────────────────
//
// Power users can add their own finance domains via Settings.custom_content_domains
// (string[] of host patterns like "https://*.example.com/*"). Because these are
// not in the manifest, we inject the content script via chrome.scripting at
// runtime — but only after asking the user to grant the host permission.

async function injectIntoCustomDomains(tabId: number, url: string): Promise<void> {
  try {
    const settings = (await getSettings()) as Record<string, unknown>;
    const customDomains = (settings.custom_content_domains as string[] | undefined) ?? [];
    if (!customDomains.length) return;

    const matches = customDomains.some((pattern) => {
      // Convert "https://*.foo.com/*" → RegExp
      const re = new RegExp(
        "^" + pattern.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*") + "$"
      );
      return re.test(url);
    });
    if (!matches) return;

    // Only inject if we have permission for this origin.
    const origin = new URL(url).origin + "/*";
    const granted = await chrome.permissions.contains({ origins: [origin] }).catch(() => false);
    if (!granted) return;

    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["content/mount.js"],
    });
  } catch (e) {
    console.warn("[agentX SW] custom domain injection failed:", e);
  }
}

chrome.tabs?.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status === "complete" && tab.url && /^https?:/.test(tab.url)) {
    void injectIntoCustomDomains(tabId, tab.url);
  }
});

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

      // Forward high-strength signals to Telegram if configured.
      // Secrets come from getSettings() which reads sensitive keys from
      // chrome.storage.local only (never sync). Prefer backend delivery
      // via /api/alerts/test; fall back to direct Telegram only when the
      // backend is unreachable.
      try {
        await forwardSignalsToTelegram(trulyNew);
      } catch (tgErr) {
        console.warn("[agentX SW] Telegram forward failed:", tgErr);
      }

      // Auto-paper-trade high-conviction signals (opt-in feedback loop)
      try {
        await autoPaperFromSignals(trulyNew);
      } catch (autoErr) {
        console.warn("[agentX SW] auto-paper failed:", autoErr);
      }
    }
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      console.warn("[agentX SW] Poll timed out — backend may be busy or unreachable");
    } else if (err instanceof TypeError && (err.message.includes("fetch") || err.message.includes("network"))) {
      console.warn("[agentX SW] Poll failed — backend not reachable");
    } else {
      console.error("[agentX SW] Poll failed:", err instanceof Error ? err.message : String(err));
    }
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
        // chrome.action.openPopup() available Chrome 127+ only — may throw on older builds
        try {
          await (chrome.action as unknown as { openPopup: () => Promise<void> }).openPopup();
        } catch { /* unsupported — silently no-op */ }
        return { ok: true };
      }
      case "PROXY_QUOTE": {
        // Content script asks the SW (which has host_permissions) to fetch quote+signal.
        try {
          const settings = await getSettings() as Record<string, string>;
          const baseUrl = settings.backend_url || "http://localhost:8020";
          const headers: Record<string, string> = { "Content-Type": "application/json" };
          if (settings.api_key) headers["X-API-Key"] = settings.api_key;
          const sym = String(message.symbol || "").trim().toUpperCase();
          if (!sym) return { ok: false, error: "No symbol" };

          const ctrl = new AbortController();
          const timer = setTimeout(() => ctrl.abort(), 10_000);
          try {
            const qRes = await fetch(`${baseUrl}/api/stocks/${encodeURIComponent(sym)}/quote`, { headers, signal: ctrl.signal });
            if (!qRes.ok) return { ok: false, error: `Quote ${qRes.status}` };
            const quote = await qRes.json();
            // Try to find a recent signal for this symbol from cached signals
            const stored = await getStoredSignals();
            const sig = stored.find((s) => s.symbol.toUpperCase() === sym && !s.dismissed) || null;
            return {
              ok: true,
              quote: { symbol: quote.symbol, price: quote.price, change_pct: quote.change_pct, name: quote.name },
              signal: sig ? { direction: sig.direction, signal_type: sig.signal_type, strength: sig.strength, reason: sig.reason } : null,
            };
          } finally {
            clearTimeout(timer);
          }
        } catch (e) {
          return { ok: false, error: e instanceof Error ? e.message : "Proxy fetch failed" };
        }
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
  // One-time migration: pull any sensitive keys out of sync into local.
  try {
    await migrateSensitiveFromSync();
  } catch (e) {
    console.warn("[agentX SW] sensitive-key migration failed:", e);
  }
  const interval = await getIntervalFromSettings();
  await registerAlarm(interval);
  await registerPaperAlarm();
  // Run initial poll immediately
  await pollSignals();

  // Right-click context menu — "Analyze in agentX"
  try {
    chrome.contextMenus.removeAll(() => {
      chrome.contextMenus.create({
        id: "agentx-analyze-selection",
        title: 'Analyze "%s" in agentX',
        contexts: ["selection"],
      });
      chrome.contextMenus.create({
        id: "agentx-open",
        title: "Open agentX",
        contexts: ["page"],
      });
    });
  } catch (e) {
    console.warn("[agentX SW] contextMenus.create failed:", e);
  }
});

// Context menu click handler
chrome.contextMenus?.onClicked.addListener(async (info) => {
  if (info.menuItemId === "agentx-analyze-selection" && info.selectionText) {
    const raw = String(info.selectionText).trim().toUpperCase();
    // Heuristic: extract a likely ticker (alphanum 2-12 chars, allow & and -)
    const match = raw.match(/^[A-Z][A-Z0-9&-]{1,11}$/) || raw.match(/[A-Z][A-Z0-9&-]{1,11}/);
    const symbol = match ? match[0] : raw.split(/\s+/)[0];
    await chrome.storage.local.set({ deepLinkTarget: { symbol, ts: Date.now() } });
    try { await (chrome.action as unknown as { openPopup: () => Promise<void> }).openPopup(); }
    catch { /* unsupported on older Chrome — handoff via deepLinkTarget will surface on next popup open */ }
  } else if (info.menuItemId === "agentx-open") {
    try { await (chrome.action as unknown as { openPopup: () => Promise<void> }).openPopup(); }
    catch { /* unsupported */ }
  }
});

// Notification click → pin signal id for the popup to consume
chrome.notifications?.onClicked.addListener(async (notificationId) => {
  const m = notificationId.match(/^signal-(.+)$/);
  if (m) {
    await chrome.storage.local.set({ pinnedSignal: { id: m[1], ts: Date.now() } });
    // Also extract symbol from stored signals for symbol-deep-link
    const signals = await getStoredSignals();
    const sig = signals.find((s) => s.id === m[1]);
    if (sig) await chrome.storage.local.set({ deepLinkTarget: { symbol: sig.symbol, ts: Date.now() } });
  }
  try { await (chrome.action as unknown as { openPopup: () => Promise<void> }).openPopup(); }
  catch { /* unsupported */ }
  try { await chrome.notifications.clear(notificationId); } catch { /* ignored */ }
});

chrome.runtime.onStartup.addListener(async () => {
  const interval = await getIntervalFromSettings();
  await registerAlarm(interval);
  await registerPaperAlarm();
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === ALARM_NAME) {
    await pollSignals();
  } else if (alarm.name === PAPER_ALARM_NAME) {
    await trackPaperTrades();
  }
});

// Open the paper tab when the user clicks an auto-close notification.
chrome.notifications?.onClicked.addListener(async (notificationId) => {
  if (notificationId.startsWith("paper-close-")) {
    await chrome.storage.local.set({ deepLinkTab: { tab: "tools", sub: "paper", ts: Date.now() } });
    try { await (chrome.action as unknown as { openPopup: () => Promise<void> }).openPopup(); }
    catch { /* unsupported */ }
  }
});
