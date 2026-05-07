/**
 * Content script — injects the floating pill into every page via Shadow DOM.
 * Shadow DOM provides full style isolation from the host page.
 * Draggable + closeable.
 */

const PILL_ID = "stockpilot-pill-root";
const STORAGE_POS_KEY = "pillPosition";
const STORAGE_HIDDEN_KEY = "pillHidden";

// ── Ticker detection on supported NSE/BSE-relevant sites ────────────
interface SiteRule {
  match: RegExp;
  /** Returns the detected ticker symbol, or null. */
  extract: () => string | null;
}

const SITE_RULES: SiteRule[] = [
  {
    // Moneycontrol stock page: /india/stockpricequote/<sector>/<co>/<code>
    match: /(^|\.)moneycontrol\.com$/i,
    extract: () => {
      const t = document.querySelector<HTMLElement>(".inid_name, .nsecp, [data-symbol]");
      const sym = t?.dataset?.symbol || t?.textContent?.trim();
      return sym ? sym.toUpperCase().split(/\s+/)[0] : null;
    },
  },
  {
    // Tickertape: /stocks/<symbol>-<id>
    match: /(^|\.)tickertape\.in$/i,
    extract: () => {
      const m = location.pathname.match(/\/stocks\/([^/?#]+)/i);
      if (!m) return null;
      // Last segment of slug is usually the ticker
      const slug = decodeURIComponent(m[1]);
      const parts = slug.toUpperCase().split("-");
      return parts[parts.length - 1] || null;
    },
  },
  {
    // Screener.in: /company/<SYMBOL>/
    match: /(^|\.)screener\.in$/i,
    extract: () => {
      const m = location.pathname.match(/\/company\/([^/]+)/i);
      return m ? decodeURIComponent(m[1]).toUpperCase() : null;
    },
  },
  {
    // TradingView: /symbols/NSE-RELIANCE/ or /chart/?symbol=NSE:RELIANCE
    match: /(^|\.)tradingview\.com$/i,
    extract: () => {
      const path = location.pathname.match(/\/symbols\/(?:NSE|BSE)[-:]([^/?#]+)/i);
      if (path) return decodeURIComponent(path[1]).toUpperCase();
      const qs = new URLSearchParams(location.search).get("symbol");
      if (qs) {
        const m = qs.match(/(?:NSE|BSE)[:\-](.+)/i);
        if (m) return m[1].toUpperCase();
      }
      return null;
    },
  },
];

function detectedTicker(): string | null {
  const host = location.hostname;
  for (const rule of SITE_RULES) {
    if (rule.match.test(host)) {
      try { return rule.extract(); } catch { return null; }
    }
  }
  return null;
}

const PANEL_ID = "agentx-inline-panel";

interface ProxyQuoteResponse {
  ok: boolean;
  quote?: { symbol: string; price: number | null; change_pct: number | null; name: string | null };
  signal?: { direction: string; signal_type: string; strength: number; reason: string } | null;
  error?: string;
}

function toggleInlinePanel(shadow: ShadowRoot, anchor: HTMLElement, symbol: string): void {
  const existing = shadow.getElementById(PANEL_ID);
  if (existing) { existing.remove(); return; }

  const panel = document.createElement("div");
  panel.id = PANEL_ID;
  panel.className = "agentx-panel";

  // Position adjacent to the pill
  const rect = anchor.getBoundingClientRect();
  const panelW = 300;
  const left = Math.max(8, Math.min(window.innerWidth - panelW - 8, rect.left - panelW - 12));
  const top = Math.max(8, Math.min(window.innerHeight - 200, rect.top));
  panel.style.left = `${left}px`;
  panel.style.top = `${top}px`;

  panel.innerHTML = `
    <div class="agentx-panel-header">
      <span style="font-weight:600;color:#E4E4E7;">📈 agentX · ${escapeHtml(symbol)}</span>
      <button class="agentx-close" aria-label="Close">✕</button>
    </div>
    <div class="agentx-panel-body">
      <div class="agentx-panel-loading">Loading…</div>
    </div>
  `;
  shadow.appendChild(panel);

  panel.querySelector(".agentx-close")?.addEventListener("click", () => panel.remove());

  // Fetch via background to avoid CORS / mixed content issues
  chrome.runtime.sendMessage({ type: "PROXY_QUOTE", symbol }, (res: ProxyQuoteResponse) => {
    const body = panel.querySelector(".agentx-panel-body");
    if (!body) return;
    if (!res || !res.ok) {
      body.innerHTML = `<div class="agentx-panel-error">${escapeHtml(res?.error || "agentX backend unreachable. Start it via ./start.sh and retry.")}</div>`;
      return;
    }
    const q = res.quote;
    const sig = res.signal;
    const stance = sig ? (sig.direction === "bullish" ? "BUY" : sig.direction === "bearish" ? "SELL" : "HOLD") : "—";
    const stanceClass = stance === "BUY" ? "buy" : stance === "SELL" ? "sell" : stance === "HOLD" ? "hold" : "";
    const pct = q?.change_pct ?? null;
    const pctColor = pct == null ? "#71717A" : pct >= 0 ? "#10B981" : "#EF4444";
    body.innerHTML = `
      <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:6px;">
        <div>
          <div style="font-size:16px;font-weight:700;color:#FAFAFA;">${q?.price != null ? "₹" + q.price.toLocaleString("en-IN") : "—"}</div>
          <div style="font-size:11px;color:${pctColor};">${pct == null ? "" : (pct >= 0 ? "+" : "") + pct.toFixed(2) + "%"}</div>
        </div>
        ${stance !== "—" ? `<span class="agentx-stance ${stanceClass}">${stance}</span>` : ""}
      </div>
      ${q?.name ? `<div class="agentx-row"><span class="label">Name</span><span style="color:#A1A1AA;text-align:right;max-width:180px;">${escapeHtml(q.name)}</span></div>` : ""}
      ${sig ? `<div class="agentx-row"><span class="label">Signal</span><span style="color:#A1A1AA;">${escapeHtml(sig.signal_type)} · ${sig.strength}/10</span></div>` : ""}
      ${sig?.reason ? `<div style="margin-top:8px;color:#D4D4D8;font-size:11px;line-height:1.5;">${escapeHtml(sig.reason)}</div>` : ""}
      <button class="agentx-btn">Open in agentX</button>
    `;
    body.querySelector(".agentx-btn")?.addEventListener("click", () => {
      chrome.storage.local.set({ deepLinkTarget: { symbol, ts: Date.now() } });
      chrome.runtime.sendMessage({ type: "OPEN_POPUP" }).catch(() => {});
    });
  });
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] as string));
}

function mount(): void {
  if (document.getElementById(PILL_ID)) return;

  // Check if user has hidden the pill
  chrome.storage.local.get(STORAGE_HIDDEN_KEY, (res) => {
    if (res[STORAGE_HIDDEN_KEY]) return; // user closed it, don't mount
    createPill();
  });
}

function createPill(): void {
  const host = document.createElement("div");
  host.id = PILL_ID;
  host.style.cssText = `
    position: fixed;
    z-index: 2147483647;
    pointer-events: none;
  `;
  document.body.appendChild(host);

  const shadow = host.attachShadow({ mode: "open" });

  const style = document.createElement("style");
  style.textContent = `
    .pill-wrap {
      position: fixed;
      pointer-events: all;
      cursor: grab;
      user-select: none;
      -webkit-user-select: none;
    }
    .pill-wrap.dragging { cursor: grabbing; }
    .pill {
      width: 52px;
      height: 52px;
      border-radius: 50%;
      background: linear-gradient(135deg, #7C3AED, #5B21B6);
      border: 2px solid #A78BFA;
      box-shadow: 0 4px 20px rgba(124, 58, 237, 0.5);
      display: flex;
      align-items: center;
      justify-content: center;
      position: relative;
      transition: transform 0.2s ease, box-shadow 0.2s ease;
    }
    .pill-wrap:not(.dragging) .pill:hover {
      transform: scale(1.08);
      box-shadow: 0 6px 28px rgba(124, 58, 237, 0.7);
    }
    .pill-wrap:not(.dragging) .pill:active { transform: scale(0.95); }
    .pill.pulse {
      animation: pulse 1.5s ease-in-out 3;
    }
    @keyframes pulse {
      0%, 100% { box-shadow: 0 4px 20px rgba(124, 58, 237, 0.5); }
      50% { box-shadow: 0 4px 32px rgba(124, 58, 237, 0.95), 0 0 0 8px rgba(124, 58, 237, 0.2); }
    }
    .pill-icon {
      width: 26px;
      height: 26px;
      fill: none;
      stroke: white;
      stroke-width: 2;
    }
    .badge {
      position: absolute;
      top: -4px;
      right: -4px;
      background: #EF4444;
      color: white;
      font-size: 11px;
      font-weight: 700;
      font-family: -apple-system, BlinkMacSystemFont, sans-serif;
      width: 20px;
      height: 20px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      border: 2px solid #18181B;
      line-height: 1;
    }
    .badge.hidden { display: none; }
    .close-btn {
      position: absolute;
      top: -6px;
      left: -6px;
      width: 18px;
      height: 18px;
      border-radius: 50%;
      background: #3F3F46;
      border: 1.5px solid #52525B;
      color: #A1A1AA;
      font-size: 11px;
      font-weight: 700;
      font-family: -apple-system, BlinkMacSystemFont, sans-serif;
      display: none;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      line-height: 1;
      pointer-events: all;
    }
    .close-btn:hover { background: #EF4444; color: white; border-color: #EF4444; }
    .pill-wrap:hover .close-btn { display: flex; }
    .pill.has-ticker::after {
      content: "";
      position: absolute;
      bottom: -2px;
      right: -2px;
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: #10B981;
      border: 2px solid #18181B;
    }
    .agentx-panel {
      position: fixed;
      width: 300px;
      background: #18181B;
      border: 1px solid #3F3F46;
      border-radius: 12px;
      box-shadow: 0 12px 40px rgba(0,0,0,0.5);
      color: #E4E4E7;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      pointer-events: all;
      z-index: 2147483646;
      overflow: hidden;
    }
    .agentx-panel-header {
      padding: 10px 12px;
      border-bottom: 1px solid #27272A;
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 11px;
    }
    .agentx-panel-body {
      padding: 12px;
      font-size: 12px;
      line-height: 1.5;
      max-height: 360px;
      overflow-y: auto;
    }
    .agentx-row { display: flex; justify-content: space-between; padding: 3px 0; font-size: 11px; }
    .agentx-row .label { color: #71717A; }
    .agentx-stance {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 6px;
      font-weight: 700;
      font-size: 11px;
    }
    .agentx-stance.buy { color: #10B981; background: rgba(16,185,129,0.12); border: 1px solid rgba(16,185,129,0.4); }
    .agentx-stance.sell { color: #EF4444; background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.4); }
    .agentx-stance.hold { color: #F59E0B; background: rgba(245,158,11,0.12); border: 1px solid rgba(245,158,11,0.4); }
    .agentx-btn {
      background: #7C3AED;
      color: white;
      border: none;
      border-radius: 6px;
      padding: 6px 10px;
      font-size: 11px;
      font-weight: 600;
      cursor: pointer;
      width: 100%;
      margin-top: 8px;
    }
    .agentx-btn:hover { background: #6D28D9; }
    .agentx-close {
      background: transparent;
      border: none;
      color: #71717A;
      cursor: pointer;
      font-size: 14px;
      padding: 0 4px;
    }
    .agentx-close:hover { color: #EF4444; }
    .agentx-panel-loading { color: #71717A; font-size: 11px; text-align: center; padding: 16px; }
    .agentx-panel-error { color: #EF4444; font-size: 11px; padding: 8px; }
  `;
  shadow.appendChild(style);

  // Wrapper for positioning
  const wrap = document.createElement("div");
  wrap.className = "pill-wrap";

  // Restore saved position or default bottom-right
  chrome.storage.local.get(STORAGE_POS_KEY, (res) => {
    const pos = res[STORAGE_POS_KEY];
    if (pos?.x != null && pos?.y != null) {
      wrap.style.left = `${Math.min(pos.x, window.innerWidth - 60)}px`;
      wrap.style.top = `${Math.min(pos.y, window.innerHeight - 60)}px`;
    } else {
      wrap.style.right = "24px";
      wrap.style.bottom = "24px";
    }
  });

  // Pill element
  const pill = document.createElement("div");
  pill.className = "pill";
  pill.title = "agentX — Click to open, drag to move";

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.classList.add("pill-icon");
  svg.innerHTML = `<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>`;
  pill.appendChild(svg);

  // Unread badge
  const badge = document.createElement("div");
  badge.className = "badge hidden";
  badge.textContent = "0";
  pill.appendChild(badge);

  // Close button
  const closeBtn = document.createElement("div");
  closeBtn.className = "close-btn";
  closeBtn.textContent = "✕";
  closeBtn.title = "Hide pill (restore from extension popup)";
  pill.appendChild(closeBtn);

  wrap.appendChild(pill);
  shadow.appendChild(wrap);

  // --- Drag logic ---
  let isDragging = false;
  let wasDragged = false;
  let startX = 0, startY = 0, origX = 0, origY = 0;

  wrap.addEventListener("pointerdown", (e: PointerEvent) => {
    if ((e.target as Element)?.classList?.contains("close-btn")) return;
    isDragging = true;
    wasDragged = false;
    wrap.classList.add("dragging");
    startX = e.clientX;
    startY = e.clientY;
    const rect = wrap.getBoundingClientRect();
    origX = rect.left;
    origY = rect.top;
    wrap.setPointerCapture(e.pointerId);
    e.preventDefault();
  });

  wrap.addEventListener("pointermove", (e: PointerEvent) => {
    if (!isDragging) return;
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) wasDragged = true;
    const newX = Math.max(0, Math.min(window.innerWidth - 60, origX + dx));
    const newY = Math.max(0, Math.min(window.innerHeight - 60, origY + dy));
    wrap.style.left = `${newX}px`;
    wrap.style.top = `${newY}px`;
    wrap.style.right = "auto";
    wrap.style.bottom = "auto";
  });

  wrap.addEventListener("pointerup", (e: PointerEvent) => {
    if (!isDragging) return;
    isDragging = false;
    wrap.classList.remove("dragging");
    wrap.releasePointerCapture(e.pointerId);
    // Save position
    const rect = wrap.getBoundingClientRect();
    chrome.storage.local.set({ [STORAGE_POS_KEY]: { x: rect.left, y: rect.top } });
  });

  // Click → if a ticker is detected on this page, toggle inline panel; else open popup
  pill.addEventListener("click", () => {
    if (wasDragged) return;
    const sym = detectedTicker();
    if (sym) {
      toggleInlinePanel(shadow, wrap, sym);
      return;
    }
    chrome.runtime.sendMessage({ type: "OPEN_POPUP" }).catch(() => {
      chrome.runtime.sendMessage({ type: "GET_EXTENSION_URL" }).then((res) => {
        if (res?.url) window.open(res.url, "_blank");
      }).catch(() => {});
    });
  });

  // Long-press / right-click on pill → always open the full popup
  pill.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    chrome.storage.local.set({ deepLinkTarget: { symbol: detectedTicker() || "", ts: Date.now() } });
    chrome.runtime.sendMessage({ type: "OPEN_POPUP" }).catch(() => {});
  });

  // If a ticker is detected on this page, give the pill a subtle indicator
  if (detectedTicker()) {
    pill.classList.add("has-ticker");
  }

  // Close button → hide pill
  closeBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    chrome.storage.local.set({ [STORAGE_HIDDEN_KEY]: true });
    host.remove();
  });

  // --- Badge updates ---
  function refreshBadge(count: number): void {
    if (count > 0) {
      badge.textContent = count > 99 ? "99+" : String(count);
      badge.classList.remove("hidden");
      pill.classList.add("pulse");
      setTimeout(() => pill.classList.remove("pulse"), 4500);
    } else {
      badge.classList.add("hidden");
    }
  }

  chrome.runtime.sendMessage({ type: "GET_UNREAD_COUNT" }, (res) => {
    if (res?.count) refreshBadge(res.count);
  });

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === "SIGNALS_UPDATED") refreshBadge(msg.count || 0);
    if (msg.type === "SHOW_PILL") {
      // Allow re-showing from extension
      chrome.storage.local.remove(STORAGE_HIDDEN_KEY);
    }
  });

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.signals) {
      const signals = (changes.signals.newValue || []) as Array<{ read: boolean; dismissed: boolean }>;
      const unread = signals.filter((s) => !s.read && !s.dismissed).length;
      refreshBadge(unread);
    }
    // If user un-hides pill from another tab
    if (area === "local" && changes[STORAGE_HIDDEN_KEY] && !changes[STORAGE_HIDDEN_KEY].newValue) {
      if (!document.getElementById(PILL_ID)) mount();
    }
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", mount);
} else {
  mount();
}
