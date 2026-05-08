import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import PortfolioSummary from "./PortfolioSummary";
import type { PortfolioSummaryData } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  api: { portfolio: { summary: vi.fn() } },
}));

vi.mock("@/lib/format", () => ({
  formatINR: (n: number) =>
    "₹" +
    new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 }).format(n),
  formatINRPrecise: (n: number) =>
    "₹" +
    new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n),
  formatPct: (n: number) => `${(n * 100).toFixed(2)}%`,
}));

vi.mock("@/components/chart/MiniSparkline", () => ({
  default: () => <div data-testid="sparkline" />,
}));

const baseData: PortfolioSummaryData = {
  invested: 1000000,
  marketValue: 1234567,
  pnl: -23456,
  pnlPct: -0.0123,
  totalValue: 1234567,
  dayPnl: 4321,
  dayPnlPct: 0.0125,
  totalPnl: -23456,
  totalPnlPct: -0.0123,
  sharpe: 1.42,
  maxDrawdown: -0.18,
  beta: 1.05,
  capital: 1000000,
  winRate: 0.6,
  profitFactor: 1.8,
  holdings: [],
  equityCurve: [],
};

describe("PortfolioSummary", () => {
  it("renders totalValue with Indian grouping", () => {
    render(<PortfolioSummary data={baseData} />);
    // 1234567 in en-IN with 0 fraction digits → "12,34,567"
    expect(screen.getByText("₹12,34,567")).toBeInTheDocument();
  });

  it("renders day P&L as up with positive arrow and percent", () => {
    render(<PortfolioSummary data={baseData} />);
    expect(screen.getByText(/↑ ₹4,321/)).toBeInTheDocument();
    expect(screen.getByText("1.25%")).toBeInTheDocument();
  });

  it("renders total P&L as negative with minus prefix and red tone", () => {
    render(<PortfolioSummary data={baseData} />);
    const node = screen.getByText(/-₹23,456/);
    expect(node).toBeInTheDocument();
    expect(node.className).toMatch(/text-rose-400/);
  });

  it("renders Sharpe to 2 decimals and beta tone", () => {
    render(<PortfolioSummary data={baseData} />);
    expect(screen.getByText("1.42")).toBeInTheDocument();
    expect(screen.getByText("1.05")).toBeInTheDocument();
  });

  it("renders sparkline when pnlSeries provided", () => {
    render(<PortfolioSummary data={baseData} pnlSeries={[1, 2, 3]} />);
    expect(screen.getByTestId("sparkline")).toBeInTheDocument();
  });

  it("does not render sparkline when series too short", () => {
    render(<PortfolioSummary data={baseData} pnlSeries={[1]} />);
    expect(screen.queryByTestId("sparkline")).not.toBeInTheDocument();
  });
});
