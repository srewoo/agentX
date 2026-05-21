import { useState } from "react";
import Earnings from "./Earnings";
import Sectors from "./Sectors";
import Holdings from "./Holdings";
import PaperTrades from "./PaperTrades";
import Screener from "./Screener";
import Performance from "./Performance";

interface Props { onSelectSymbol?: (symbol: string) => void; }

type SubTab = "screener" | "sectors" | "earnings" | "holdings" | "paper" | "perf";

const TABS: Array<{ id: SubTab; label: string; icon: string }> = [
  // Screener owns the dividend / momentum / value / growth presets — that's
  // where the legacy "dividend stocks" list lives.
  { id: "screener", label: "Screener", icon: "📊" },
  { id: "sectors", label: "Sectors", icon: "🗺️" },
  { id: "earnings", label: "Earnings", icon: "📅" },
  { id: "holdings", label: "Holdings", icon: "💼" },
  { id: "paper", label: "Paper", icon: "📒" },
  { id: "perf", label: "Perf", icon: "📈" },
];

export default function Tools({ onSelectSymbol }: Props) {
  const [tab, setTab] = useState<SubTab>("screener");

  return (
    <div className="flex flex-col h-full">
      <div className="flex border-b border-border">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`flex-1 py-1.5 text-[11px] font-medium border-b-2 transition-colors ${
              tab === t.id ? "text-brand-light border-brand bg-zinc-900/40"
                : "text-zinc-500 border-transparent hover:text-zinc-300"
            }`}
          >
            {t.icon} {t.label}
          </button>
        ))}
      </div>
      <div className="flex-1 min-h-0">
        {tab === "screener" && <Screener onSelectSymbol={onSelectSymbol} />}
        {tab === "sectors" && <Sectors />}
        {tab === "earnings" && <Earnings onSelectSymbol={onSelectSymbol} />}
        {tab === "holdings" && <Holdings onSelectSymbol={onSelectSymbol} />}
        {tab === "paper" && <PaperTrades onSelectSymbol={onSelectSymbol} />}
        {tab === "perf" && <Performance />}
      </div>
    </div>
  );
}
