import { useState, useCallback } from "react";
import Dashboard from "./pages/Dashboard";
import Search from "./pages/Search";
import Screener from "./pages/Screener";
import Watchlist from "./pages/Watchlist";
import Alerts from "./pages/Alerts";
import Settings from "./pages/Settings";
import { ErrorBoundary } from "./components/ErrorBoundary";

type Tab = "dashboard" | "search" | "screener" | "watchlist" | "alerts" | "settings";

const TABS: { id: Tab; label: string; icon: string }[] = [
  { id: "dashboard", label: "Signals", icon: "⚡" },
  { id: "search", label: "Search", icon: "🔍" },
  { id: "screener", label: "Screener", icon: "📊" },
  { id: "watchlist", label: "Watchlist", icon: "★" },
  { id: "alerts", label: "Alerts", icon: "🔔" },
  { id: "settings", label: "Settings", icon: "⚙" },
];

export default function App() {
  const [activeTab, setActiveTab] = useState<Tab>("dashboard");
  const [searchSymbol, setSearchSymbol] = useState<string | null>(null);

  const handleScreenerSelect = useCallback((symbol: string) => {
    setSearchSymbol(symbol);
    setActiveTab("search");
  }, []);

  return (
    <div className="flex flex-col bg-surface text-zinc-100" style={{ width: 527, height: 600, maxHeight: 600, overflow: "hidden" }}>
      {/* Header */}
      <div className="flex-shrink-0 flex items-center justify-between px-4 py-2 border-b border-border bg-panel">
        <div className="flex items-center gap-2">
          <span className="text-brand text-lg">📈</span>
          <span className="font-semibold text-sm text-zinc-100">agentX</span>
        </div>
        <span className="text-xs text-zinc-500">NSE/BSE Copilot</span>
      </div>

      {/* Content — each tab wrapped in an ErrorBoundary; key resets on tab switch */}
      <div className="flex-1 overflow-hidden min-h-0">
        <ErrorBoundary key={activeTab}>
          {activeTab === "dashboard" && <Dashboard />}
          {activeTab === "search" && <Search initialSymbol={searchSymbol} onSymbolConsumed={() => setSearchSymbol(null)} />}
          {activeTab === "screener" && <Screener onSelectSymbol={handleScreenerSelect} />}
          {activeTab === "watchlist" && <Watchlist />}
          {activeTab === "alerts" && <Alerts />}
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
    </div>
  );
}
