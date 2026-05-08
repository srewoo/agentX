import { useEffect, useState } from "react";
import { apiClient, ApiError } from "../lib/api";
import type { LlmUsage } from "../lib/types";

const POLL_MS = 60_000;

export function useLlmUsage() {
  const [data, setData] = useState<LlmUsage | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();

    async function load() {
      try {
        const next = await apiClient.getLlmUsage(ctrl.signal);
        if (!cancelled) {
          setData(next);
          setError(null);
        }
      } catch (err) {
        if (cancelled) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        setError(err instanceof ApiError ? err : new ApiError(String(err), 0));
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    }

    void load();
    const id = setInterval(load, POLL_MS);

    return () => {
      cancelled = true;
      ctrl.abort();
      clearInterval(id);
    };
  }, []);

  return { data, error, isLoading };
}
