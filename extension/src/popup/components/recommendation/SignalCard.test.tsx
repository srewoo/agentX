import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import SignalCard from "./SignalCard";
import type { RecommendationView } from "./types";

function makeRec(overrides: Partial<RecommendationView> = {}): RecommendationView {
  return {
    id: "rec-001",
    symbol: "RELIANCE",
    name: "Reliance Industries",
    exchange: "NSE",
    sector: "Energy",
    horizon: "swing",
    direction: "BUY",
    conviction: 0.78, // 0..1 scale
    rationale: [
      "Breakout above 50-DMA on rising volume",
      "FII inflow into Energy sector last 3 sessions",
      "RSI 62 — momentum still has room",
    ],
    entryPrice: 2500,
    stopLoss: 2400,
    target: 2700,
    generatedAt: "2026-05-08T04:30:00.000Z",
    // Extended optional surface
    target2: 2850,
    riskReward: 2.0,
    marketCapBand: "LARGE",
    lastPrice: 2524.8,
    priceChangePct1d: 1.34,
    deliveryPct: 48,
    fiiDiiSignal: "INFLOW",
    fAndOSignal: "LONG_BUILDUP",
    signals: [
      { name: "trend", weight: 0.9, value: 0.8, direction: "pos" },
      { name: "momentum", weight: 0.8, value: 0.7, direction: "pos" },
      { name: "volume", weight: 0.6, value: 0.5, direction: "pos" },
    ],
    ...overrides,
  };
}

describe("SignalCard (recommendation)", () => {
  it("renders symbol, exchange, action, and conviction", () => {
    render(<SignalCard recommendation={makeRec()} />);
    expect(screen.getByRole("heading", { name: "RELIANCE" })).toBeInTheDocument();
    expect(screen.getByText("NSE")).toBeInTheDocument();
    expect(screen.getByText("BUY")).toBeInTheDocument();
    // Conviction gauge exposes role=img with aria-label normalised to 0..100
    expect(screen.getByRole("img", { name: /Conviction 78 out of 100/ })).toBeInTheDocument();
  });

  it("renders SELL action when direction is SELL", () => {
    render(<SignalCard recommendation={makeRec({ direction: "SELL" })} />);
    expect(screen.getByText("SELL")).toBeInTheDocument();
  });

  it("renders the top reasons", () => {
    render(<SignalCard recommendation={makeRec()} />);
    expect(screen.getByText(/Breakout above 50-DMA/)).toBeInTheDocument();
    expect(screen.getByText(/FII inflow into Energy/)).toBeInTheDocument();
  });

  it("renders R:R chip and stoploss tag", () => {
    render(<SignalCard recommendation={makeRec()} />);
    expect(screen.getByText(/R:R 1:2\.00/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Stoploss/)).toBeInTheDocument();
  });

  it("renders FII flows badge and F&O badge when present", () => {
    render(<SignalCard recommendation={makeRec()} />);
    expect(screen.getByLabelText(/FII\/DII inflow/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/F and O: Long Buildup/i)).toBeInTheDocument();
  });

  it("omits flows / F&O / delivery chips when not provided", () => {
    render(
      <SignalCard
        recommendation={makeRec({
          fiiDiiSignal: undefined,
          fAndOSignal: undefined,
          deliveryPct: undefined,
        })}
      />
    );
    expect(screen.queryByLabelText(/FII\/DII/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/F and O/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Delivery/)).not.toBeInTheDocument();
  });

  it("invokes onSelect on click", () => {
    const onSelect = vi.fn();
    render(<SignalCard recommendation={makeRec()} onSelect={onSelect} />);
    fireEvent.click(screen.getByTestId("rec-signal-card"));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect.mock.calls[0]?.[0]?.symbol).toBe("RELIANCE");
  });

  it("invokes onSelect on Enter key", () => {
    const onSelect = vi.fn();
    render(<SignalCard recommendation={makeRec()} onSelect={onSelect} />);
    fireEvent.keyDown(screen.getByTestId("rec-signal-card"), { key: "Enter" });
    expect(onSelect).toHaveBeenCalled();
  });

  it("invokes onSelect on Space key", () => {
    const onSelect = vi.fn();
    render(<SignalCard recommendation={makeRec()} onSelect={onSelect} />);
    fireEvent.keyDown(screen.getByTestId("rec-signal-card"), { key: " " });
    expect(onSelect).toHaveBeenCalled();
  });

  it("does not become a button when onSelect is missing", () => {
    render(<SignalCard recommendation={makeRec()} />);
    // The card root must not be a button when non-interactive. Inline
    // "Watch" / "Alert" actions are still buttons, so we check the card
    // root specifically rather than the page-wide button query.
    expect(screen.getByTestId("rec-signal-card")).not.toHaveAttribute("role", "button");
  });

  it("aria-label summarises action and conviction", () => {
    render(<SignalCard recommendation={makeRec()} onSelect={() => {}} />);
    const card = screen.getByTestId("rec-signal-card");
    const label = card.getAttribute("aria-label") ?? "";
    expect(label).toMatch(/RELIANCE/);
    expect(label).toMatch(/BUY/);
    expect(label).toMatch(/conviction 78/);
    expect(label).toMatch(/risk reward 2\.00/);
  });

  it("falls back gracefully when entry/stop/target are null", () => {
    render(
      <SignalCard
        recommendation={makeRec({ entryPrice: null, stopLoss: null, target: null, riskReward: undefined })}
      />
    );
    expect(screen.getByText(/Levels pending/)).toBeInTheDocument();
    expect(screen.queryByText(/R:R/)).not.toBeInTheDocument();
  });
});
