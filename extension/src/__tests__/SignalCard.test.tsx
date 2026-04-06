import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import SignalCard from "../popup/components/SignalCard";
import type { Signal } from "../shared/types";

// Mock the MiniChart component (uses lightweight-charts which needs canvas)
vi.mock("../popup/components/MiniChart", () => ({
  default: () => <div data-testid="mini-chart">Chart</div>,
}));

// Mock the api module
vi.mock("../shared/api", () => ({
  api: {
    markRead: vi.fn(async () => ({ ok: true })),
  },
}));

function makeSignal(overrides: Partial<Signal> = {}): Signal {
  return {
    id: "sig-001",
    symbol: "RELIANCE",
    signal_type: "price_spike",
    direction: "bullish",
    strength: 7,
    reason: "Price moved +5.2% in the last session",
    risk: "Price spikes can reverse quickly.",
    llm_summary: "AI analysis: strong momentum driven by sector rotation.",
    current_price: 2524.8,
    metadata: {},
    created_at: new Date().toISOString(),
    read: false,
    dismissed: false,
    ...overrides,
  };
}

describe("SignalCard", () => {
  const onRead = vi.fn();
  const onDismiss = vi.fn();

  it("should render symbol and action badge", () => {
    render(<SignalCard signal={makeSignal()} onRead={onRead} onDismiss={onDismiss} />);
    expect(screen.getByText("RELIANCE")).toBeInTheDocument();
    expect(screen.getByText("BUY")).toBeInTheDocument();
  });

  it("should show SELL for bearish signals", () => {
    render(
      <SignalCard
        signal={makeSignal({ direction: "bearish" })}
        onRead={onRead}
        onDismiss={onDismiss}
      />
    );
    expect(screen.getByText("SELL")).toBeInTheDocument();
  });

  it("should show HOLD for neutral signals", () => {
    render(
      <SignalCard
        signal={makeSignal({ direction: "neutral" })}
        onRead={onRead}
        onDismiss={onDismiss}
      />
    );
    expect(screen.getByText("HOLD")).toBeInTheDocument();
  });

  it("should display current price formatted in INR", () => {
    render(<SignalCard signal={makeSignal()} onRead={onRead} onDismiss={onDismiss} />);
    expect(screen.getByText(/2,524.8/)).toBeInTheDocument();
  });

  it("should show unread indicator when not read", () => {
    const { container } = render(
      <SignalCard signal={makeSignal({ read: false })} onRead={onRead} onDismiss={onDismiss} />
    );
    // Unread dot has specific class
    expect(container.querySelector(".bg-brand-light.rounded-full")).toBeInTheDocument();
  });

  it("should not show unread indicator when read", () => {
    const { container } = render(
      <SignalCard signal={makeSignal({ read: true })} onRead={onRead} onDismiss={onDismiss} />
    );
    expect(container.querySelector(".bg-brand-light.rounded-full")).not.toBeInTheDocument();
  });

  it("should display reason text", () => {
    render(<SignalCard signal={makeSignal()} onRead={onRead} onDismiss={onDismiss} />);
    expect(screen.getByText(/Price moved \+5\.2%/)).toBeInTheDocument();
  });

  it("should display signal type label", () => {
    render(<SignalCard signal={makeSignal()} onRead={onRead} onDismiss={onDismiss} />);
    expect(screen.getByText("Price Spike")).toBeInTheDocument();
  });

  it("should expand on click and show LLM summary", () => {
    render(<SignalCard signal={makeSignal()} onRead={onRead} onDismiss={onDismiss} />);

    // LLM summary not visible initially
    expect(screen.queryByText(/AI analysis/)).not.toBeInTheDocument();

    // Click to expand
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText(/AI analysis/)).toBeInTheDocument();
  });

  it("should show risk when expanded", () => {
    render(<SignalCard signal={makeSignal()} onRead={onRead} onDismiss={onDismiss} />);
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText(/Price spikes can reverse quickly/)).toBeInTheDocument();
  });

  it("should call onRead when unread card is expanded", () => {
    render(<SignalCard signal={makeSignal({ read: false })} onRead={onRead} onDismiss={onDismiss} />);
    fireEvent.click(screen.getByRole("button"));
    expect(onRead).toHaveBeenCalledWith("sig-001");
  });

  it("should call onDismiss when dismiss button is clicked", () => {
    render(<SignalCard signal={makeSignal()} onRead={onRead} onDismiss={onDismiss} />);
    // Expand first
    fireEvent.click(screen.getByRole("button"));
    // Click dismiss
    fireEvent.click(screen.getByLabelText(/Dismiss RELIANCE signal/));
    expect(onDismiss).toHaveBeenCalledWith("sig-001");
  });

  it("should show MiniChart when expanded", () => {
    render(<SignalCard signal={makeSignal()} onRead={onRead} onDismiss={onDismiss} />);
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByTestId("mini-chart")).toBeInTheDocument();
  });

  it("should display strength as visual bar", () => {
    const { container } = render(
      <SignalCard signal={makeSignal({ strength: 8 })} onRead={onRead} onDismiss={onDismiss} />
    );
    // 5 bar segments always rendered
    const bars = container.querySelectorAll(".w-1\\.5.h-3.rounded-sm");
    expect(bars.length).toBe(5);
  });

  it("should show timeframe badge", () => {
    render(<SignalCard signal={makeSignal({ signal_type: "price_spike" })} onRead={onRead} onDismiss={onDismiss} />);
    expect(screen.getByText("Intraday")).toBeInTheDocument();
  });

  it("should have correct aria-label for accessibility", () => {
    render(<SignalCard signal={makeSignal()} onRead={onRead} onDismiss={onDismiss} />);
    const button = screen.getByRole("button");
    expect(button).toHaveAttribute("aria-label", expect.stringContaining("RELIANCE"));
    expect(button).toHaveAttribute("aria-label", expect.stringContaining("BUY"));
  });

  it("should handle keyboard expand with Enter", () => {
    render(<SignalCard signal={makeSignal()} onRead={onRead} onDismiss={onDismiss} />);
    fireEvent.keyDown(screen.getByRole("button"), { key: "Enter" });
    expect(screen.getByText(/AI analysis/)).toBeInTheDocument();
  });

  // ─────────────────────────────────────────────
  // New badge tests
  // ─────────────────────────────────────────────

  it("should render confluence badge with signal count", () => {
    render(
      <SignalCard
        signal={makeSignal({
          signal_type: "confluence",
          metadata: { signal_count: 3, contributing_signals: ["rsi_extreme", "macd_crossover", "volume_spike"] },
        })}
        onRead={onRead}
        onDismiss={onDismiss}
      />
    );
    expect(screen.getByText("3x CONFLUENCE")).toBeInTheDocument();
  });

  it("should render FII SELLING indicator when fii_modifier is negative", () => {
    render(
      <SignalCard
        signal={makeSignal({
          metadata: { fii_modifier: -2, fii_net: -2000 },
        })}
        onRead={onRead}
        onDismiss={onDismiss}
      />
    );
    expect(screen.getByText("FII SELLING")).toBeInTheDocument();
  });

  it("should render FII BUYING indicator when fii_modifier is positive", () => {
    render(
      <SignalCard
        signal={makeSignal({
          metadata: { fii_modifier: 2, fii_net: 2000 },
        })}
        onRead={onRead}
        onDismiss={onDismiss}
      />
    );
    expect(screen.getByText("FII BUYING")).toBeInTheDocument();
  });

  it("should not render FII badge when fii_modifier is zero", () => {
    render(
      <SignalCard
        signal={makeSignal({
          metadata: { fii_modifier: 0 },
        })}
        onRead={onRead}
        onDismiss={onDismiss}
      />
    );
    expect(screen.queryByText("FII SELLING")).not.toBeInTheDocument();
    expect(screen.queryByText("FII BUYING")).not.toBeInTheDocument();
  });

  it("should render delivery % badge for volume spike with high delivery", () => {
    render(
      <SignalCard
        signal={makeSignal({
          signal_type: "volume_spike",
          direction: "neutral",
          metadata: { delivery_pct: 72, volume_ratio: 3.5 },
        })}
        onRead={onRead}
        onDismiss={onDismiss}
      />
    );
    expect(screen.getByText("72% Delivery")).toBeInTheDocument();
  });

  it("should render delivery % badge for low delivery volume spike", () => {
    render(
      <SignalCard
        signal={makeSignal({
          signal_type: "volume_spike",
          direction: "neutral",
          metadata: { delivery_pct: 18 },
        })}
        onRead={onRead}
        onDismiss={onDismiss}
      />
    );
    expect(screen.getByText("18% Delivery")).toBeInTheDocument();
  });

  it("should not render delivery badge for non-volume-spike signals", () => {
    render(
      <SignalCard
        signal={makeSignal({
          signal_type: "price_spike",
          metadata: { delivery_pct: 72 },
        })}
        onRead={onRead}
        onDismiss={onDismiss}
      />
    );
    expect(screen.queryByText("72% Delivery")).not.toBeInTheDocument();
  });

  it("should render MIXED badge for conflicting signals", () => {
    render(
      <SignalCard
        signal={makeSignal({
          metadata: { conflicting_signals: true },
        })}
        onRead={onRead}
        onDismiss={onDismiss}
      />
    );
    expect(screen.getByText("MIXED")).toBeInTheDocument();
  });

  it("should render new signal type labels", () => {
    const types = [
      { type: "rsi_divergence", label: "RSI Divergence" },
      { type: "macd_divergence", label: "MACD Divergence" },
      { type: "options_flow", label: "Options Flow" },
    ] as const;
    for (const { type, label } of types) {
      const { unmount } = render(
        <SignalCard
          signal={makeSignal({ signal_type: type as any })}
          onRead={onRead}
          onDismiss={onDismiss}
        />
      );
      expect(screen.getByText(label)).toBeInTheDocument();
      unmount();
    }
  });
});
