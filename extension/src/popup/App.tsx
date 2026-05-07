import { useState, useEffect, useCallback } from "react";
import Dashboard from "./pages/Dashboard";
import Search from "./pages/Search";
import Screener from "./pages/Screener";
import Watchlist from "./pages/Watchlist";
import Alerts from "./pages/Alerts";
import Settings from "./pages/Settings";
import Tools from "./pages/Tools";
import Onboarding from "./components/Onboarding";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { getSettings } from "../shared/storage";
import { deepLink } from "../shared/localStore";
import type { AppSettings } from "../shared/types";

type Tab = "dashboard" | "search" | "screener" | "watchlist" | "alerts" | "tools" | "settings";

const TABS: { id: Tab; label: string; icon: string }[] = [
  { id: "dashboard", label: "Signals", icon: "⚡" },
  { id: "search", label: "Search", icon: "🔍" },
  { id: "screener", label: "Screener", icon: "📊" },
  { id: "watchlist", label: "Watchlist", icon: "★" },
  { id: "alerts", label: "Alerts", icon: "🔔" },
  { id: "tools", label: "Tools", icon: "🧰" },
  { id: "settings", label: "Settings", icon: "⚙" },
];

export default function App() {
  const [activeTab, setActiveTab] = useState<Tab>("dashboard");
  const [searchSymbol, setSearchSymbol] = useState<string | null>(null);
  // Default to *not* showing onboarding — flip to true only after we confirm
  // first-run state. This lets the main UI render synchronously (and keeps
  // existing tests that render <App /> and immediately assert on tabs working).
  const [showOnboarding, setShowOnboarding] = useState<boolean>(false);
  const [theme, setTheme] = useState<"dark" | "light">("dark");

  const handleScreenerSelect = useCallback((symbol: string) => {
    setSearchSymbol(symbol);
    setActiveTab("search");
  }, []);

  const isStandalone = typeof window !== "undefined"
    && new URLSearchParams(window.location.search).get("standalone") === "1";

  const handlePopOut = useCallback(() => {
    const url = chrome.runtime.getURL("popup/index.html") + "?standalone=1";
    if (chrome.windows?.create) {
      chrome.windows.create({ url, type: "popup", width: 1100, height: 800 });
    } else {
      chrome.tabs.create({ url });
    }
    window.close();
  }, []);

  // First-run check + deep-link consumption + theme
  useEffect(() => {
    (async () => {
      const settings = (await getSettings()) as Partial<AppSettings>;
      setShowOnboarding(!settings.onboarding_complete);
      setTheme(settings.theme === "light" ? "light" : "dark");

      // Deep-link from right-click menu or content script
      const dl = await deepLink.consume();
      if (dl) {
        setSearchSymbol(dl);
        setActiveTab("search");
      }
    })();

    // Listen for theme changes broadcast from settings
    const onChange = (changes: { [k: string]: chrome.storage.StorageChange }, area: string) => {
      if (area !== "sync") return;
      if (changes.settings) {
        const s = (changes.settings.newValue as Partial<AppSettings>) || {};
        if (s.theme) setTheme(s.theme === "light" ? "light" : "dark");
      }
    };
    chrome.storage.onChanged.addListener(onChange);
    return () => chrome.storage.onChanged.removeListener(onChange);
  }, []);

  // Apply theme class on document
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  return (
    <div
      className="relative flex flex-col bg-surface text-zinc-100"
      style={
        isStandalone
          ? { width: "100%", height: "100%", overflow: "hidden" }
          : { width: 527, height: 600, maxHeight: 600, overflow: "hidden" }
      }
      data-theme={theme}
    >
      {/* Header */}
      <div className="flex-shrink-0 flex items-center justify-between px-4 py-2 border-b border-border bg-panel">
        <div className="flex items-center gap-2">
          <span className="text-brand text-lg">📈</span>
          <span className="font-semibold text-sm text-zinc-100">agentX</span>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-xs text-zinc-500">NSE/BSE Copilot</span>
          {!isStandalone && (
            <button
              onClick={handlePopOut}
              title="Pop out to a standalone window"
              aria-label="Pop out to a standalone window"
              className="text-zinc-400 hover:text-brand-light text-base leading-none px-1"
            >
              ⛶
            </button>
          )}
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-hidden min-h-0">
        <ErrorBoundary key={activeTab}>
          {activeTab === "dashboard" && <Dashboard onSelectSymbol={handleScreenerSelect} />}
          {activeTab === "search" && <Search initialSymbol={searchSymbol} onSymbolConsumed={() => setSearchSymbol(null)} />}
          {activeTab === "screener" && <Screener onSelectSymbol={handleScreenerSelect} />}
          {activeTab === "watchlist" && <Watchlist onSelectSymbol={handleScreenerSelect} />}
          {activeTab === "alerts" && <Alerts />}
          {activeTab === "tools" && <Tools onSelectSymbol={handleScreenerSelect} />}
          {activeTab === "settings" && <Settings />}
        </ErrorBoundary>
      </div>

      {/* Bottom Tab Bar */}
      <div className="flex-shrink-0 flex border-t border-border bg-panel">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`tab-btn flex-1 flex flex-col items-center gap-0.5 py-2 text-[10px]
              ${activeTab === tab.id
                ? "text-brand-light border-t-2 border-brand"
                : "text-zinc-500 border-t-2 border-transparent hover:text-zinc-300"
              }`}
          >
            <span className="text-base leading-none">{tab.icon}</span>
            <span>{tab.label}</span>
          </button>
        ))}
      </div>

      {showOnboarding && <Onboarding onDone={() => setShowOnboarding(false)} />}
    </div>
  );
}
