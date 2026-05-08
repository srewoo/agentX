import type { ReactNode } from "react";

interface EmptyStateProps {
  title: string;
  body?: string;
  icon?: string;
  action?: ReactNode;
}

export function EmptyState({ title, body, icon = "○", action }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center text-center px-6 py-10 gap-2">
      <div
        aria-hidden="true"
        className="w-10 h-10 rounded-full flex items-center justify-center text-lg"
        style={{ background: "var(--bg-panel-hover)", color: "var(--text-secondary)" }}
      >
        {icon}
      </div>
      <h3 className="text-sm font-semibold tk-text">{title}</h3>
      {body && <p className="text-xs tk-text-muted max-w-[280px]">{body}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}
