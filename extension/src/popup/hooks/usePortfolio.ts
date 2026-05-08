import { useEffect, useRef, useState } from "react";
import { apiClient, ApiError } from "../lib/api";
import type { PortfolioSummary } from "../lib/types";

const POLL_MS = 60_000;

export function usePortfolio() {
  const [data, setData] = useState<PortfolioSummary | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isFetching, setIsFetching] = useState(false);
  const cancelRef = useRef(false);

  useEffect(() => {
    cancelRef.current = false;
    const ctrl = new AbortController();

    async function load(initial: boolean) {
      if (initial) setIsLoading(true);
      setIsFetching(true);
      try {
        const next = await apiClient.getPortfolio(ctrl.signal);
        if (cancelRef.current) return;
        setData(next);
        setError(null);
      } catch (err) {
        if (cancelRef.current) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        setError(err instanceof ApiError ? err : new ApiError(String(err), 0));
      } finally {
        if (!cancelRef.current) {
          setIsLoading(false);
          setIsFetching(false);
        }
      }
    }

    void load(true);
    const id = setInterval(() => void load(false), POLL_MS);
    return () => {
      cancelRef.current = true;
      ctrl.abort();
      clearInterval(id);
    };
  }, []);

  return { data, error, isLoading, isFetching };
}
