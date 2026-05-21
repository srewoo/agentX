// WatchlistEditor
// ---------------------------------------------------------------------------
// Chip-input search with NSE-symbol autocomplete from `/api/stocks/search`.
// Drag-to-reorder uses the native HTML5 DnD API.
//
// Accessibility:
//  • combobox pattern for the search input (aria-controls, aria-activedescendant)
//  • each chip has a visible Remove button — never relies on hover
//  • keyboard shortcuts inside chip list:
//      ArrowUp / ArrowDown — move focus to neighbouring chip
//      Alt+ArrowUp / Alt+ArrowDown — reorder current chip
//      Backspace / Delete — remove
//  • drag handle is a real <button> with role="button" + keyboard equivalents

import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { TextInput } from "./_primitives";

export interface WatchlistRow {
  symbol: string;
  name: string;
  exchange?: string;
}

interface SearchResult {
  symbol: string;
  name: string;
  exchange: string;
}

interface Props {
  items: WatchlistRow[];
  onChange: (next: WatchlistRow[]) => Promise<void>;
  // Caller wires this to api.search(q).
  searchSymbols: (q: string) => Promise<SearchResult[]>;
}

const DEBOUNCE_MS = 250;

export default function WatchlistEditor({ items, onChange, searchSymbols }: Props) {
  const inputId = useId();
  const listboxId = useId();
  const manualSymbolId = useId();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Manual entry — covers BSE-only listings the NSE-indexed search misses.
  const [manualSymbol, setManualSymbol] = useState("");
  const [manualExchange, setManualExchange] = useState<"NSE" | "BSE">("NSE");

  const addManual = async () => {
    const sym = manualSymbol.trim().toUpperCase();
    if (!sym) return;
    if (items.some((i) => i.symbol.toUpperCase() === sym && (i.exchange ?? "NSE") === manualExchange)) {
      return;
    }
    await onChange([...items, { symbol: sym, name: sym, exchange: manualExchange }]);
    setManualSymbol("");
  };

  const existingSymbols = useMemo(
    () => new Set(items.map((i) => i.symbol.toUpperCase())),
    [items],
  );

  // Debounced search
  useEffect(() => {
    const q = query.trim();
    if (q.length < 1) {
      setResults([]);
      setActiveIndex(-1);
      return;
    }
    const timer = setTimeout(async () => {
      setLoading(true);
      setError(null);
      try {
        const r = await searchSymbols(q);
        setResults(r.filter((x) => !existingSymbols.has(x.symbol.toUpperCase())));
        setActiveIndex(0);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Search failed.");
        setResults([]);
      } finally {
        setLoading(false);
      }
    }, DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [query, searchSymbols, existingSymbols]);

  const addSymbol = async (r: SearchResult) => {
    const next = [
      ...items,
      { symbol: r.symbol, name: r.name, exchange: r.exchange },
    ];
    await onChange(next);
    setQuery("");
    setResults([]);
    setActiveIndex(-1);
    inputRef.current?.focus();
  };

  const removeAt = async (idx: number) => {
    const next = items.filter((_, i) => i !== idx);
    await onChange(next);
  };

  const move = async (from: number, to: number) => {
    if (to < 0 || to >= items.length || from === to) return;
    const next = [...items];
    const [m] = next.splice(from, 1);
    next.splice(to, 0, m);
    await onChange(next);
  };

  // Keyboard handling for the combobox input
  const onInputKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (results.length === 0 && e.key === "Backspace" && query === "" && items.length > 0) {
      // Quick-remove the last chip when input is empty.
      void removeAt(items.length - 1);
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((i) => Math.min(i + 1, results.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (activeIndex >= 0 && results[activeIndex]) {
        void addSymbol(results[activeIndex]);
      }
    } else if (e.key === "Escape") {
      setQuery("");
      setResults([]);
      setActiveIndex(-1);
    }
  };

  return (
    <div className="rounded-md border border-slate-800 bg-slate-900/60 p-3 flex flex-col gap-3">
      <h3 className="text-sm font-medium text-slate-100">Watchlist</h3>

      {/* Chips */}
      {items.length === 0 ? (
        <p className="text-xs text-slate-500">
          No symbols yet. Search below to add stocks (NSE).
        </p>
      ) : (
        <ul
          aria-label="Watchlist symbols"
          className="flex flex-wrap gap-1.5"
        >
          {items.map((item, idx) => (
            <Chip
              key={item.symbol}
              item={item}
              index={idx}
              total={items.length}
              onRemove={() => void removeAt(idx)}
              onMove={(to) => void move(idx, to)}
            />
          ))}
        </ul>
      )}

      {/* Combobox */}
      <div className="relative">
        <label htmlFor={inputId} className="text-xs font-medium text-slate-300">
          Add symbol
        </label>
        <div className="mt-1">
          <TextInput
            id={inputId}
            ref={inputRef}
            type="search"
            role="combobox"
            aria-expanded={results.length > 0}
            aria-controls={listboxId}
            aria-autocomplete="list"
            aria-activedescendant={
              activeIndex >= 0 ? `${listboxId}-opt-${activeIndex}` : undefined
            }
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onInputKeyDown}
            placeholder="Search NSE symbols (e.g., RELIANCE)"
            autoComplete="off"
          />
        </div>
        {results.length > 0 ? (
          <ul
            id={listboxId}
            role="listbox"
            className="absolute z-10 mt-1 w-full max-h-60 overflow-y-auto rounded-md border border-slate-700 bg-slate-900 shadow-lg"
          >
            {results.map((r, i) => (
              <li
                key={r.symbol}
                id={`${listboxId}-opt-${i}`}
                role="option"
                aria-selected={i === activeIndex}
                className={[
                  "px-2 py-1.5 text-sm cursor-pointer",
                  i === activeIndex
                    ? "bg-emerald-700 text-white"
                    : "text-slate-200 hover:bg-slate-800",
                ].join(" ")}
                onMouseDown={(e) => {
                  // Use mousedown so input blur doesn't kill the click.
                  e.preventDefault();
                  void addSymbol(r);
                }}
              >
                <span className="font-mono">{r.symbol}</span>
                <span className="ml-2 text-xs text-slate-400">{r.name}</span>
              </li>
            ))}
          </ul>
        ) : null}
        {loading ? (
          <p className="mt-1 text-xs text-slate-500">Searching…</p>
        ) : error ? (
          <p role="alert" className="mt-1 text-xs text-rose-400">
            {error}
          </p>
        ) : null}
      </div>

      {/* Manual add — needed for BSE-only listings (SME segment, smaller mid/
          small caps) that aren't in the NSE-indexed search. */}
      <div className="border-t border-slate-800 pt-3">
        <label htmlFor={manualSymbolId} className="text-xs font-medium text-slate-300">
          Add by symbol + exchange
        </label>
        <div className="mt-1 flex gap-1.5">
          <TextInput
            id={manualSymbolId}
            value={manualSymbol}
            onChange={(e) => setManualSymbol(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void addManual(); } }}
            placeholder="Symbol (e.g., SHANTIGEAR)"
            autoComplete="off"
            className="flex-1"
          />
          <select
            value={manualExchange}
            onChange={(e) => setManualExchange(e.target.value as "NSE" | "BSE")}
            className="bg-slate-800 border border-slate-700 rounded px-2 text-xs text-slate-100"
            aria-label="Exchange"
          >
            <option value="NSE">NSE</option>
            <option value="BSE">BSE</option>
          </select>
          <button
            type="button"
            onClick={() => void addManual()}
            disabled={!manualSymbol.trim()}
            className="text-xs px-2 py-1 rounded bg-emerald-700 text-white disabled:opacity-40"
          >
            Add
          </button>
        </div>
        <p className="mt-1 text-[10px] text-slate-500">
          Use this for BSE-only listings the search misses.
        </p>
      </div>
    </div>
  );
}

// ── Chip ──────────────────────────────────────────────────────────────────

function Chip({
  item,
  index,
  total,
  onRemove,
  onMove,
}: {
  item: WatchlistRow;
  index: number;
  total: number;
  onRemove: () => void;
  onMove: (to: number) => void;
}) {
  const [dragOver, setDragOver] = useState(false);
  const liRef = useRef<HTMLLIElement>(null);

  const onKeyDown = (e: KeyboardEvent<HTMLLIElement>) => {
    if (e.altKey && e.key === "ArrowUp") {
      e.preventDefault();
      onMove(index - 1);
    } else if (e.altKey && e.key === "ArrowDown") {
      e.preventDefault();
      onMove(index + 1);
    } else if (e.key === "Backspace" || e.key === "Delete") {
      e.preventDefault();
      onRemove();
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      const prev = liRef.current?.previousElementSibling as HTMLElement | null;
      prev?.focus();
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      const next = liRef.current?.nextElementSibling as HTMLElement | null;
      next?.focus();
    }
  };

  return (
    <li
      ref={liRef}
      tabIndex={0}
      onKeyDown={onKeyDown}
      draggable
      onDragStart={(e) => {
        e.dataTransfer.setData("text/plain", String(index));
        e.dataTransfer.effectAllowed = "move";
      }}
      onDragOver={(e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDragOver(false);
        const from = Number(e.dataTransfer.getData("text/plain"));
        if (Number.isFinite(from) && from !== index) onMove(index);
      }}
      aria-label={`${item.symbol} ${item.name}, position ${index + 1} of ${total}. Use Alt+Arrow to reorder, Delete to remove.`}
      className={[
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs",
        dragOver
          ? "border-emerald-400 bg-emerald-900/40"
          : "border-slate-700 bg-slate-800 text-slate-200",
        "focus:outline-none focus:ring-2 focus:ring-emerald-400",
      ].join(" ")}
    >
      <span className="font-mono">{item.symbol}</span>
      <button
        type="button"
        onClick={onRemove}
        aria-label={`Remove ${item.symbol} from watchlist`}
        className="text-slate-400 hover:text-rose-400 focus:outline-none focus:ring-1 focus:ring-rose-400 rounded"
      >
        ×
      </button>
    </li>
  );
}

