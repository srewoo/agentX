import { useState, useCallback, useRef, useEffect } from "react";
import StockQuoteCard from "../components/StockQuote";
import AnalysisPanel from "../components/AnalysisPanel";
import MiniChart from "../components/MiniChart";
import { api } from "../../shared/api";
import type { StockQuote, AIAnalysisResponse } from "../../shared/types";

interface SearchResult {
  symbol: string;
  name: string;
  exchange: string;
}

interface SearchProps {
  initialSymbol?: string | null;
  onSymbolConsumed?: () => void;
}

function useDebounce<T extends unknown[]>(fn: (...args: T) => void, delay: number) {
  const timer = useRef<ReturnType<typeof setTimeout>>(null);
  return useCallback((...args: T) => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => fn(...args), delay);
  }, [fn, delay]);
}

export default function Search({ initialSymbol, onSymbolConsumed }: SearchProps = {}) {
  const [query, setQuery] = useState("");
  const [suggestions, setSuggestions] = useState<SearchResult[]>([]);
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null);
  const [quote, setQuote] = useState<StockQuote | null>(null);
  const [analysis, setAnalysis] = useState<AIAnalysisResponse | null>(null);
  const [loadingQuote, setLoadingQuote] = useState(false);
  const [loadingAnalysis, setLoadingAnalysis] = useState(false);
  const [timeframe, setTimeframe] = useState<"intraday" | "swing" | "long">("swing");
  const [error, setError] = useState<string | null>(null);

  // Handle navigation from Screener with a pre-selected symbol
  useEffect(() => {
    if (initialSymbol) {
      selectSymbol(initialSymbol);
      if (onSymbolConsumed) onSymbolConsumed();
    }
  }, [initialSymbol]); // eslint-disable-line react-hooks/exhaustive-deps

  const searchSuggestions = useCallback(async (q: string) => {
    if (q.length < 1) { setSuggestions([]); return; }
    try {
      const res = await api.search(q);
      setSuggestions(res.results.slice(0, 6));
    } catch {
      setSuggestions([]);
    }
  }, []);

  const debouncedSearch = useDebounce(searchSuggestions, 300);

  const handleInput = (e: React.ChangeEvent<HTMLInputElement>) => {
    const val = e.target.value;
    setQuery(val);
    debouncedSearch(val);
  };

  const selectSymbol = async (symbol: string) => {
    setSelectedSymbol(symbol);
    setQuery(symbol);
    setSuggestions([]);
    setAnalysis(null);
    setError(null);
    setLoadingQuote(true);
    try {
      const q = await api.getQuote(symbol);
      setQuote(q);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoadingQuote(false);
    }
  };

  const runAnalysis = async () => {
    if (!selectedSymbol) return;
    setLoadingAnalysis(true);
    setError(null);
    try {
      const res = await api.aiAnalysis(selectedSymbol, timeframe);
      setAnalysis(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Analysis failed. Check your LLM API key in Settings.");
    } finally {
      setLoadingAnalysis(false);
    }
  };

  return (
    <div className="flex flex-col h-full">
      {/* Search bar */}
      <div className="px-3 pt-3 pb-2 relative">
        <input
          type="text"
          value={query}
          onChange={handleInput}
          placeholder="Search stock (e.g. TCS, RELIANCE...)"
          className="w-full bg-zinc-800 border border-border rounded-lg px-3 py-2 text-sm text-zinc-100 placeholder-zinc-500 focus:outline-none focus:border-brand"
        />
        {suggestions.length > 0 && (
          <div className="absolute left-3 right-3 top-full bg-zinc-800 border border-border rounded-lg z-10 overflow-hidden shadow-xl">
            {suggestions.map((s) => (
              <button
                key={s.symbol}
                onClick={() => selectSymbol(s.symbol)}
                className="w-full text-left px-3 py-2 text-sm hover:bg-zinc-700 flex items-center justify-between"
              >
                <span className="font-medium text-zinc-100">{s.symbol}</span>
                <span className="text-zinc-500 text-xs truncate max-w-[180px]">{s.name}</span>
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto px-3 pb-3 space-y-3">
        {error && (
          <div className="text-xs text-loss bg-loss/10 border border-loss/30 rounded p-2">
            {error}
          </div>
        )}

        {loadingQuote && (
          <div className="text-xs text-zinc-500 text-center py-4">Loading quote...</div>
        )}

        {quote && !loadingQuote && (
          <>
            <StockQuoteCard quote={quote} />

            {/* Price chart */}
            <MiniChart symbol={selectedSymbol!} height={200} />

            {/* Timeframe selector + AI analysis button */}
            {!analysis && (
              <div className="flex gap-2">
                <div className="flex rounded-lg border border-border overflow-hidden">
                  <button
                    onClick={() => setTimeframe("intraday")}
                    className={`px-2.5 py-1.5 text-xs font-medium ${timeframe === "intraday" ? "bg-brand text-white" : "text-zinc-400 hover:text-zinc-200"}`}
                  >
                    Intraday
                  </button>
                  <button
                    onClick={() => setTimeframe("swing")}
                    className={`px-2.5 py-1.5 text-xs font-medium border-x border-border ${timeframe === "swing" ? "bg-brand text-white" : "text-zinc-400 hover:text-zinc-200"}`}
                  >
                    Swing
                  </button>
                  <button
                    onClick={() => setTimeframe("long")}
                    className={`px-2.5 py-1.5 text-xs font-medium ${timeframe === "long" ? "bg-brand text-white" : "text-zinc-400 hover:text-zinc-200"}`}
                  >
                    Long-term
                  </button>
                </div>
                <button
                  onClick={runAnalysis}
                  disabled={loadingAnalysis}
                  className="flex-1 bg-brand text-white rounded-lg text-xs font-medium py-1.5 hover:bg-brand/80 disabled:opacity-50"
                >
                  {loadingAnalysis ? "Analyzing..." : "Run AI Analysis"}
                </button>
              </div>
            )}

            {loadingAnalysis && (
              <div className="text-xs text-zinc-500 text-center py-4">
                Running AI analysis... This may take 10-15 seconds.
              </div>
            )}

            {analysis && (
              <>
                <AnalysisPanel result={analysis} />
                <button
                  onClick={() => setAnalysis(null)}
                  className="text-xs text-zinc-500 hover:text-zinc-300 w-full text-center"
                >
                  Run again
                </button>
              </>
            )}
          </>
        )}

        {!selectedSymbol && !loadingQuote && (
          <div className="flex flex-col items-center justify-center h-[400px] gap-3 text-zinc-500">
            <span className="text-4xl">🔍</span>
            <p className="text-sm text-center">
              Search for any NSE/BSE stock to get a live quote and AI analysis
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
