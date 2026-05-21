import { createContext, useContext, type ReactNode } from "react";
import type { Exchange } from "./types";

/**
 * App-level exchange selection — driven by the NSE/BSE toggle in the Header.
 *
 * Lifting this into context (rather than prop-drilling) lets any tab read the
 * current exchange without each parent having to forward it. Per-row exchange
 * (e.g. a watchlist item that's specifically BSE) still takes priority — the
 * context is only the default for "uncommitted" UI like Search or the live
 * chart on a generic symbol lookup.
 */
interface ExchangeContextValue {
  exchange: Exchange;
  setExchange: (e: Exchange) => void;
}

const ExchangeContext = createContext<ExchangeContextValue | undefined>(undefined);

export function ExchangeProvider({
  value,
  children,
}: {
  value: ExchangeContextValue;
  children: ReactNode;
}) {
  return <ExchangeContext.Provider value={value}>{children}</ExchangeContext.Provider>;
}

/** Read the current selection. Defaults to NSE outside the provider so test
 *  renders of leaf components don't have to wrap in the provider. */
export function useExchange(): Exchange {
  const ctx = useContext(ExchangeContext);
  return ctx?.exchange ?? "NSE";
}

/** Read both the value and the setter — only the Header needs the setter. */
export function useExchangeControl(): ExchangeContextValue {
  const ctx = useContext(ExchangeContext);
  return ctx ?? { exchange: "NSE", setExchange: () => undefined };
}
