import { useEffect, useState } from "react";
import { getMarketStatus } from "../lib/marketStatus";
import type { MarketStatus } from "../lib/types";

/** Recomputes on a 30s tick — IST clock changes are slow enough. */
export function useMarketStatus(): MarketStatus {
  const [status, setStatus] = useState<MarketStatus>(() => getMarketStatus());
  useEffect(() => {
    const id = setInterval(() => setStatus(getMarketStatus()), 30_000);
    return () => clearInterval(id);
  }, []);
  return status;
}
