/**
 * Content script — injects the floating pill into every page via Shadow DOM.
 * Shadow DOM provides full style isolation from the host page.
 * Draggable + closeable.
 */

const PILL_ID = "stockpilot-pill-root";
const STORAGE_POS_KEY = "pillPosition";
const STORAGE_HIDDEN_KEY = "pillHidden";

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

  // Click → open popup (only if not dragged)
  pill.addEventListener("click", () => {
    if (wasDragged) return;
    chrome.runtime.sendMessage({ type: "OPEN_POPUP" }).catch(() => {
      chrome.runtime.sendMessage({ type: "GET_EXTENSION_URL" }).then((res) => {
        if (res?.url) window.open(res.url, "_blank");
      }).catch(() => {});
    });
  });

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
