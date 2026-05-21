import { useEffect, useState } from "react";
import { api } from "../../shared/api";
import type { NewsItem } from "../../shared/types";

interface Props {
  collapsedDefault?: boolean;
  filterSymbols?: string[]; // optional: only show news matching these symbols
}

function fmt(iso?: string) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  const diff = Date.now() - d.getTime();
  const m = Math.floor(diff / 60000);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  return `${Math.floor(h / 24)}d`;
}

function sentimentColor(s?: number): string {
  if (s == null) return "text-zinc-500";
  if (s > 0.2) return "text-profit";
  if (s < -0.2) return "text-loss";
  return "text-zinc-400";
}

export default function NewsPanel({ collapsedDefault = true, filterSymbols }: Props) {
  const [collapsed, setCollapsed] = useState(collapsedDefault);
  const [items, setItems] = useState<NewsItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (collapsed) return;
    let cancelled = false;
    const fetchNews = () => {
      api.getNews(20)
        .then((r) => { if (!cancelled) setItems(r.news || []); })
        .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : "News unavailable"); });
    };
    fetchNews();
    // Refetch every hour while the panel is open so the user sees fresh
    // headlines without having to close and reopen the popup.
    const interval = setInterval(fetchNews, 60 * 60 * 1000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [collapsed]);

  const filterSet = filterSymbols ? new Set(filterSymbols.map((s) => s.toUpperCase())) : null;
  const visible = items
    ? filterSet
      ? items.filter((n) => (n.symbols || []).some((s) => filterSet.has(s.toUpperCase())))
      : items
    : [];

  return (
    <div className="border-b border-border bg-zinc-900/30">
      <button
        onClick={() => setCollapsed((v) => !v)}
        className="w-full flex items-center justify-between px-3 py-1.5 text-[10px] text-zinc-400 uppercase tracking-wider hover:text-zinc-200"
      >
        <span>📰 Market News {visible.length > 0 ? `(${visible.length})` : ""}</span>
        <span className="text-zinc-600">{collapsed ? "▼" : "▲"}</span>
      </button>
      {!collapsed && (
        <div className="px-3 pb-2 max-h-44 overflow-y-auto">
          {error && <div className="text-[11px] text-loss py-2">{error}</div>}
          {!error && items === null && <div className="text-[11px] text-zinc-500 py-2">Loading…</div>}
          {!error && items !== null && visible.length === 0 && (
            <div className="text-[11px] text-zinc-500 py-2">No news.</div>
          )}
          {visible.map((n, i) => (
            <a
              key={`${n.url || n.title}-${i}`}
              href={n.url || "#"}
              target="_blank"
              rel="noopener noreferrer"
              className="block py-1.5 border-b border-border/40 last:border-0 hover:bg-zinc-800/40 -mx-1 px-1 rounded"
            >
              <div className="flex items-start gap-2">
                <span className={`text-[10px] mt-0.5 ${sentimentColor(n.sentiment)}`}>
                  {n.sentiment != null ? (n.sentiment > 0.2 ? "▲" : n.sentiment < -0.2 ? "▼" : "•") : "•"}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="text-[11px] text-zinc-200 leading-snug line-clamp-2">{n.title}</div>
                  <div className="flex items-center gap-1.5 mt-0.5 text-[10px] text-zinc-600">
                    {n.source && <span>{n.source}</span>}
                    {n.published_at && <span>· {fmt(n.published_at)} ago</span>}
                    {n.symbols && n.symbols.length > 0 && (
                      <span className="text-brand-light">· {n.symbols.slice(0, 3).join(", ")}</span>
                    )}
                  </div>
                </div>
              </div>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}
