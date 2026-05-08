import { Suspense, type ReactNode } from "react";

interface TabPanelProps {
  id: string;
  active: boolean;
  children: ReactNode;
}

export function TabPanel({ id, active, children }: TabPanelProps) {
  if (!active) return null;
  return (
    <div
      id={`panel-${id}`}
      role="tabpanel"
      aria-labelledby={`nav-${id}`}
      className="h-full overflow-auto"
    >
      <Suspense fallback={<TabFallback />}>{children}</Suspense>
    </div>
  );
}

export function TabFallback() {
  return (
    <div className="p-4 space-y-2" aria-busy="true" aria-live="polite">
      <div className="h-4 rounded animate-pulse" style={{ background: "var(--bg-panel-hover)" }} />
      <div className="h-20 rounded animate-pulse" style={{ background: "var(--bg-panel-hover)" }} />
      <div className="h-20 rounded animate-pulse" style={{ background: "var(--bg-panel-hover)" }} />
    </div>
  );
}
