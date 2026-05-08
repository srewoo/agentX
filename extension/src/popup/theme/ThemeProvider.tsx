import { createContext, useContext, useEffect, useState, useCallback, type ReactNode } from "react";

export type ThemeMode = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

interface ThemeContextValue {
  mode: ThemeMode;
  resolved: ResolvedTheme;
  setMode: (m: ThemeMode) => void;
  toggle: () => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

function resolveSystem(): ResolvedTheme {
  if (typeof window === "undefined" || !window.matchMedia) return "dark";
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

interface ThemeProviderProps {
  children: ReactNode;
  /** Optional controlled mode. Falls back to internal state. */
  mode?: ThemeMode;
  onModeChange?: (m: ThemeMode) => void;
}

export function ThemeProvider({ children, mode: controlledMode, onModeChange }: ThemeProviderProps) {
  const [internalMode, setInternalMode] = useState<ThemeMode>(controlledMode ?? "dark");
  const mode = controlledMode ?? internalMode;
  const [resolved, setResolved] = useState<ResolvedTheme>(() =>
    mode === "system" ? resolveSystem() : (mode as ResolvedTheme),
  );

  const setMode = useCallback(
    (next: ThemeMode) => {
      if (controlledMode === undefined) setInternalMode(next);
      onModeChange?.(next);
    },
    [controlledMode, onModeChange],
  );

  const toggle = useCallback(() => {
    setMode(resolved === "dark" ? "light" : "dark");
  }, [resolved, setMode]);

  // React to system changes when in system mode.
  useEffect(() => {
    if (mode !== "system") {
      setResolved(mode as ResolvedTheme);
      return;
    }
    setResolved(resolveSystem());
    if (!window.matchMedia) return;
    const mq = window.matchMedia("(prefers-color-scheme: light)");
    const onChange = () => setResolved(mq.matches ? "light" : "dark");
    mq.addEventListener?.("change", onChange);
    return () => mq.removeEventListener?.("change", onChange);
  }, [mode]);

  // Apply to <html data-theme>.
  useEffect(() => {
    if (typeof document === "undefined") return;
    document.documentElement.dataset.theme = resolved;
  }, [resolved]);

  return (
    <ThemeContext.Provider value={{ mode, resolved, setMode, toggle }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) {
    // Sensible fallback for tests that render without provider.
    return {
      mode: "dark",
      resolved: "dark",
      setMode: () => undefined,
      toggle: () => undefined,
    };
  }
  return ctx;
}
