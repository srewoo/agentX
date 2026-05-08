import { useCallback, useEffect, useRef, useState } from "react";
import { apiClient, ApiError } from "../lib/api";
import type { Recommendation, RecommendationFilters } from "../lib/types";

interface State {
  data: Recommendation[] | null;
  error: ApiError | null;
  isLoading: boolean;
  isFetching: boolean;
  lastFetchedAt: number | null;
}

const INTRADAY_STALE_MS = 30_000;
const SWING_STALE_MS = 5 * 60_000;

export function useRecommendations(filters: RecommendationFilters = {}) {
  const [state, setState] = useState<State>({
    data: null,
    error: null,
    isLoading: true,
    isFetching: false,
    lastFetchedAt: null,
  });
  const filtersKey = JSON.stringify(filters);

  // Stable filters ref so refetch always sees latest.
  const filtersRef = useRef(filters);
  filtersRef.current = filters;

  const refetch = useCallback(async () => {
    const ctrl = new AbortController();
    setState((s) => ({ ...s, isFetching: true, error: null }));
    try {
      const data = await apiClient.getRecommendations(filtersRef.current, ctrl.signal);
      setState({
        data,
        error: null,
        isLoading: false,
        isFetching: false,
        lastFetchedAt: Date.now(),
      });
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      const apiErr = err instanceof ApiError ? err : new ApiError(String(err), 0);
      setState((s) => ({ ...s, isLoading: false, isFetching: false, error: apiErr }));
    }
    return () => ctrl.abort();
  }, []);

  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    setState((s) => ({ ...s, isLoading: s.data === null, isFetching: true, error: null }));
    apiClient
      .getRecommendations(filters, ctrl.signal)
      .then((data) => {
        if (cancelled) return;
        setState({
          data,
          error: null,
          isLoading: false,
          isFetching: false,
          lastFetchedAt: Date.now(),
        });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        const apiErr = err instanceof ApiError ? err : new ApiError(String(err), 0);
        setState((s) => ({ ...s, isLoading: false, isFetching: false, error: apiErr }));
      });

    // Background revalidation cadence based on horizon.
    const staleMs =
      filters.horizon === "intraday" ? INTRADAY_STALE_MS : SWING_STALE_MS;
    const interval = setInterval(() => {
      void refetch();
    }, staleMs);

    return () => {
      cancelled = true;
      ctrl.abort();
      clearInterval(interval);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filtersKey, refetch]);

  return { ...state, refetch };
}
