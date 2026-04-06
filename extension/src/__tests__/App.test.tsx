import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import App from "../popup/App";

// Mock all page components to avoid pulling in heavy dependencies
vi.mock("../popup/pages/Dashboard", () => ({
  default: () => <div data-testid="page-dashboard">Dashboard</div>,
}));
vi.mock("../popup/pages/Search", () => ({
  default: ({ initialSymbol }: { initialSymbol: string | null }) => (
    <div data-testid="page-search">Search {initialSymbol ?? ""}</div>
  ),
}));
vi.mock("../popup/pages/Screener", () => ({
  default: ({ onSelectSymbol }: { onSelectSymbol: (s: string) => void }) => (
    <div data-testid="page-screener">
      Screener
      <button onClick={() => onSelectSymbol("TCS")}>Select TCS</button>
    </div>
  ),
}));
vi.mock("../popup/pages/Watchlist", () => ({
  default: () => <div data-testid="page-watchlist">Watchlist</div>,
}));
vi.mock("../popup/pages/Alerts", () => ({
  default: () => <div data-testid="page-alerts">Alerts</div>,
}));
vi.mock("../popup/pages/Settings", () => ({
  default: () => <div data-testid="page-settings">Settings</div>,
}));

describe("App", () => {
  it("should render the header with app name", () => {
    render(<App />);
    expect(screen.getByText("agentX")).toBeInTheDocument();
    expect(screen.getByText("NSE/BSE Copilot")).toBeInTheDocument();
  });

  it("should show Dashboard tab by default", () => {
    render(<App />);
    expect(screen.getByTestId("page-dashboard")).toBeInTheDocument();
  });

  it("should render all 6 tab buttons", () => {
    render(<App />);
    expect(screen.getByText("Signals")).toBeInTheDocument();
    expect(screen.getByText("Search")).toBeInTheDocument();
    expect(screen.getByText("Screener")).toBeInTheDocument();
    expect(screen.getByText("Watchlist")).toBeInTheDocument();
    expect(screen.getByText("Alerts")).toBeInTheDocument();
    expect(screen.getByText("Settings")).toBeInTheDocument();
  });

  it("should switch to Search tab when clicked", () => {
    render(<App />);
    fireEvent.click(screen.getByText("Search"));
    expect(screen.getByTestId("page-search")).toBeInTheDocument();
    expect(screen.queryByTestId("page-dashboard")).not.toBeInTheDocument();
  });

  it("should switch to Watchlist tab when clicked", () => {
    render(<App />);
    fireEvent.click(screen.getByText("Watchlist"));
    expect(screen.getByTestId("page-watchlist")).toBeInTheDocument();
  });

  it("should switch to Alerts tab when clicked", () => {
    render(<App />);
    fireEvent.click(screen.getByText("Alerts"));
    expect(screen.getByTestId("page-alerts")).toBeInTheDocument();
  });

  it("should switch to Settings tab when clicked", () => {
    render(<App />);
    fireEvent.click(screen.getByText("Settings"));
    expect(screen.getByTestId("page-settings")).toBeInTheDocument();
  });

  it("should navigate from Screener to Search when a symbol is selected", () => {
    render(<App />);
    // Go to Screener
    fireEvent.click(screen.getByText("Screener"));
    expect(screen.getByTestId("page-screener")).toBeInTheDocument();

    // Select TCS from screener
    fireEvent.click(screen.getByText("Select TCS"));

    // Should navigate to Search with the symbol
    expect(screen.getByTestId("page-search")).toBeInTheDocument();
    expect(screen.getByText(/TCS/)).toBeInTheDocument();
  });
});
