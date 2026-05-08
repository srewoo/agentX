import { type KeyboardEvent } from "react";

export interface NavItem<T extends string> {
  id: T;
  label: string;
  icon: string;
}

interface BottomNavProps<T extends string> {
  items: ReadonlyArray<NavItem<T>>;
  active: T;
  onChange: (id: T) => void;
}

export function BottomNav<T extends string>({ items, active, onChange }: BottomNavProps<T>) {
  function onKey(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const idx = items.findIndex((i) => i.id === active);
    if (idx < 0) return;
    e.preventDefault();
    const nextIdx =
      e.key === "ArrowRight"
        ? (idx + 1) % items.length
        : (idx - 1 + items.length) % items.length;
    onChange(items[nextIdx].id);
    const btn = document.getElementById(`nav-${items[nextIdx].id}`);
    btn?.focus();
  }

  return (
    <nav
      role="tablist"
      aria-label="Primary"
      onKeyDown={onKey}
      className="flex-shrink-0 flex border-t tk-border tk-bg-panel"
      style={{ borderTopColor: "var(--border-default)" }}
    >
      {items.map((tab) => {
        const selected = active === tab.id;
        return (
          <button
            key={tab.id}
            id={`nav-${tab.id}`}
            role="tab"
            type="button"
            aria-selected={selected}
            aria-controls={`panel-${tab.id}`}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(tab.id)}
            className="flex-1 flex flex-col items-center gap-0.5 py-2 text-[10px] transition-colors"
            style={{
              color: selected ? "var(--accent-saffron)" : "var(--text-muted)",
              borderTop: `2px solid ${selected ? "var(--accent-saffron)" : "transparent"}`,
            }}
          >
            <span aria-hidden="true" className="text-base leading-none">{tab.icon}</span>
            <span>{tab.label}</span>
          </button>
        );
      })}
    </nav>
  );
}
