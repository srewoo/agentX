import { lazy, useCallback, useEffect, useState } from "react";
import { ErrorBoundary } from "./components/ErrorBoundary";
import Onboarding from "./components/Onboarding";
import { Header } from "./layout/Header";
import { BottomNav, type NavItem } from "./layout/BottomNav";
import { TabPanel } from "./layout/TabPanel";
import { ThemeProvider, type ThemeMode } from "./theme/ThemeProvider";
import { getSettings } from "../shared/storage";
import { deepLink } from "../shared/localStore";
import type { AppSettings } from "../shared/types";
import type { Exchange } from "./lib/types";
import "./theme/tokens.css";

// Lazy-loaded tab views — keeps the popup chunk small and the
// initial paint fast (only the active tab is parsed/executed).
const LiveView = lazy(() => import("./pages/Dashboard"));
const ToolsView = lazy(() => import("./pages/Tools"));
const SearchView = lazy(() => import("./pages/Search"));
const WatchlistView = lazy(() => import("./pages/Watchlist"));
const PortfolioView = lazy(() => import("./views/PortfolioView"));
const AlertsView = lazy(() => import("./pages/Alerts"));
const SettingsView = lazy(() => import("./views/SettingsView"));

type TabId =
  | "live"
  | "search"
  | "tools"
  | "watchlist"
  | "portfolio"
  | "alerts"
  | "settings";

// "Live" carries indices (NIFTY/SENSEX/BANKNIFTY) + recommendations, "Tools"
// owns sectors/earnings/holdings/paper-trading. Portfolio is its own tab now
// that the type-shape mismatch is resolved. Watchlist/Alerts use the legacy
// pages so the existing add/remove flows continue to work.
const TABS: ReadonlyArray<NavItem<TabId>> = [
  { id: "live", label: "Live", icon: "◉" },
  { id: "search", label: "Search", icon: "🔍" },
  { id: "tools", label: "Tools", icon: "🧰" },
  { id: "watchlist", label: "Watchlist", icon: "★" },
  { id: "portfolio", label: "Portfolio", icon: "◧" },
  { id: "alerts", label: "Alerts", icon: "🔔" },
  { id: "settings", label: "Settings", icon: "⚙" },
];

export default function App() {
  const [active, setActive] = useState<TabId>("live");
  const [exchange, setExchange] = useState<Exchange>("NSE");
  const [showOnboarding, setShowOnboarding] = useState(false);
  const [themeMode, setThemeMode] = useState<ThemeMode>("dark");
  // Symbol carried into the Search tab from a deep-link or a click in
  // Live/Signals/Watchlist. Cleared once the Search page consumes it.
  const [searchSeed, setSearchSeed] = useState<string | null>(null);

  const handleSelectSymbol = useCallback((symbol: string) => {
    setSearchSeed(symbol);
    setActive("search");
  }, []);

  const isStandalone =
    typeof window !== "undefined" &&
    new URLSearchParams(window.location.search).get("standalone") === "1";

  const handlePopOut = useCallback(() => {
    const url = chrome.runtime.getURL("popup/index.html") + "?standalone=1";
    if (chrome.windows?.create) {
      chrome.windows.create({ url, type: "popup", width: 1100, height: 800 });
    } else {
      chrome.tabs.create({ url });
    }
    window.close();
  }, []);

  // Load settings (theme, onboarding, deep-link) on mount.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const settings = (await getSettings()) as Partial<AppSettings>;
        if (cancelled) return;
        setShowOnboarding(!settings.onboarding_complete);
        if (settings.theme === "light" || settings.theme === "dark") {
          setThemeMode(settings.theme);
        }
        const dl = await deepLink.consume();
        if (!cancelled && dl) {
          setSearchSeed(dl);
          setActive("search");
        }
      } catch {
        // Storage may be unavailable in odd test contexts — fail open.
      }
    })();

    const onChange = (
      changes: { [k: string]: chrome.storage.StorageChange },
      area: string,
    ) => {
      if (area !== "sync" || !changes.settings) return;
      const next = (changes.settings.newValue as Partial<AppSettings>) || {};
      if (next.theme === "light" || next.theme === "dark") setThemeMode(next.theme);
    };
    chrome.storage?.onChanged?.addListener(onChange);
    return () => {
      cancelled = true;
      chrome.storage?.onChanged?.removeListener(onChange);
    };
  }, []);

  const sizeStyle = isStandalone
    ? { width: "100%", height: "100%", overflow: "hidden" as const }
    : { width: 527, height: 600, maxHeight: 600, overflow: "hidden" as const };

  return (
    <ThemeProvider mode={themeMode} onModeChange={setThemeMode}>
      <div
        className="relative flex flex-col"
        style={{
          ...sizeStyle,
          background: "var(--bg-app)",
          color: "var(--text-primary)",
        }}
      >
        <Header
          exchange={exchange}
          onExchangeChange={setExchange}
          showPopOut={!isStandalone}
          onPopOut={handlePopOut}
        />

        <main className="flex-1 overflow-hidden min-h-0">
          <ErrorBoundary key={active}>
            <TabPanel id="live" active={active === "live"}>
              <LiveView onSelectSymbol={handleSelectSymbol} />
            </TabPanel>
            <TabPanel id="search" active={active === "search"}>
              <SearchView
                initialSymbol={searchSeed}
                onSymbolConsumed={() => setSearchSeed(null)}
              />
            </TabPanel>
            <TabPanel id="tools" active={active === "tools"}>
              <ToolsView onSelectSymbol={handleSelectSymbol} />
            </TabPanel>
            <TabPanel id="watchlist" active={active === "watchlist"}>
              <WatchlistView onSelectSymbol={handleSelectSymbol} />
            </TabPanel>
            <TabPanel id="portfolio" active={active === "portfolio"}>
              <PortfolioView />
            </TabPanel>
            <TabPanel id="alerts" active={active === "alerts"}>
              <AlertsView />
            </TabPanel>
            <TabPanel id="settings" active={active === "settings"}>
              <SettingsView />
            </TabPanel>
          </ErrorBoundary>
        </main>

        <BottomNav items={TABS} active={active} onChange={setActive} />

        {showOnboarding && <Onboarding onDone={() => setShowOnboarding(false)} />}
      </div>
    </ThemeProvider>
  );
}
