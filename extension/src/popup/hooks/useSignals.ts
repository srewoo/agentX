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

      // 2. Fetch fresh signals from backend API. The backend is authoritative
      //    for what is CURRENT — it already applies the age cutoff and dedup —
      //    so a successful response REPLACES the feed, even when it's empty.
      //    (Previously we only merged when fresh.length > 0 and never removed
      //    anything, so aged-out signals lingered in local storage forever and
      //    a quiet scan kept showing last week's cards.) We carry over only the
      //    local read/dismissed flags by id so marking-read isn't lost.
      try {
        const res = await api.getSignals(undefined, 50);
        const fresh = res.signals || [];
        const flagsById = new Map(stored.map((s) => [s.id, s]));
        const reconciled = fresh
          .map((s) => {
            const prev = flagsById.get(s.id);
            return prev
              ? { ...s, read: s.read || prev.read, dismissed: s.dismissed || prev.dismissed }
              : s;
          })
          .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
          .slice(0, MAX_SIGNALS);

        await setStoredSignals(reconciled);
        setSignals(reconciled.filter((s) => !s.dismissed));
        setError(null);
      } catch (fetchErr) {
        // Backend unreachable — stale storage is a reasonable offline fallback.
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
