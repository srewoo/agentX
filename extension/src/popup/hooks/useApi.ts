import { useState, useCallback } from "react";

export function useApi<T>(fn: (...args: unknown[]) => Promise<T>) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const execute = useCallback(async (...args: unknown[]) => {
    setLoading(true);
    setError(null);
    try {
      const result = await fn(...args);
      setData(result);
      return result;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return null;
    } finally {
      setLoading(false);
    }
  }, [fn]);

  const reset = useCallback(() => {
    setData(null);
    setError(null);
  }, []);

  return { data, loading, error, execute, reset };
}
