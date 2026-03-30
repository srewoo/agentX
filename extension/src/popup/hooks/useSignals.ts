import { useState, useEffect, useCallback } from "react";
import type { Signal } from "../../shared/types";
import { getStoredSignals, setStoredSignals } from "../../shared/storage";
import { api } from "../../shared/api";

const MAX_SIGNALS = 200;

export function useSignals() {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      // 1. Show stored signals immediately (instant UX)
      const stored = await getStoredSignals();
      setSignals(stored.filter((s) => !s.dismissed));
      setLoading(false);

      // 2. Fetch fresh signals from backend API and merge
      try {
        const res = await api.getSignals(undefined, 50);
        const fresh = res.signals || [];

        if (fresh.length > 0) {
          // Merge: fresh signals take priority, dedup by ID
          const existingById = new Map(stored.map((s) => [s.id, s]));
          for (const s of fresh) {
            existingById.set(s.id, s);
          }
          const merged = Array.from(existingById.values())
            .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
            .slice(0, MAX_SIGNALS);

          await setStoredSignals(merged);
          setSignals(merged.filter((s) => !s.dismissed));
        }
        setError(null);
      } catch (fetchErr) {
        // Backend unreachable — stale storage is fine, just show warning
        setError("Could not reach backend. Showing cached signals.");
      }
    } catch (e) {
      setError(String(e));
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();

    // Re-load when storage changes (service worker pushed updates)
    const listener = (changes: Record<string, chrome.storage.StorageChange>, area: string) => {
      if (area === "local" && changes.signals) {
        const updated = (changes.signals.newValue || []) as Signal[];
        setSignals(updated.filter((s) => !s.dismissed));
      }
    };
    chrome.storage.onChanged.addListener(listener);
    return () => chrome.storage.onChanged.removeListener(listener);
  }, [load]);

  const markRead = useCallback(async (id: string) => {
    setSignals((prev) => prev.map((s) => (s.id === id ? { ...s, read: true } : s)));
    chrome.runtime.sendMessage({ type: "MARK_READ", signalId: id }).catch(() => {});
    api.markRead(id).catch(() => {}); // Best-effort sync to backend
  }, []);

  const markAllRead = useCallback(async () => {
    setSignals((prev) => prev.map((s) => ({ ...s, read: true })));
    chrome.runtime.sendMessage({ type: "MARK_ALL_READ" }).catch(() => {});
    api.markAllRead().catch(() => {});
  }, []);

  const dismiss = useCallback(async (id: string) => {
    setSignals((prev) => prev.filter((s) => s.id !== id));
    chrome.runtime.sendMessage({ type: "DISMISS_SIGNAL", signalId: id }).catch(() => {});
    api.dismissSignal(id).catch(() => {});
  }, []);

  const unreadCount = signals.filter((s) => !s.read).length;

  return { signals, loading, error, unreadCount, markRead, markAllRead, dismiss, reload: load };
}
