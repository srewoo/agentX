import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import App from "../popup/App";

// Mock all view components — tabs are lazy-loaded, so we replace each with
// a simple test stub to avoid pulling charts, queries and the API surface.
vi.mock("../popup/pages/Dashboard", () => ({
  default: () => <div data-testid="view-live">Live</div>,
}));
vi.mock("../popup/pages/Tools", () => ({
  default: () => <div data-testid="view-tools">Tools</div>,
}));
vi.mock("../popup/pages/Search", () => ({
  default: () => <div data-testid="view-search">Search</div>,
}));
vi.mock("../popup/pages/Watchlist", () => ({
  default: () => <div data-testid="view-watchlist">Watchlist</div>,
}));
vi.mock("../popup/views/PortfolioView", () => ({
  default: () => <div data-testid="view-portfolio">Portfolio</div>,
}));
vi.mock("../popup/pages/Alerts", () => ({
  default: () => <div data-testid="view-alerts">Alerts</div>,
}));
vi.mock("../popup/views/SettingsView", () => ({
  default: () => <div data-testid="view-settings">Settings</div>,
}));

describe("App", () => {
  it("renders the header", () => {
    render(<App />);
    expect(screen.getByText("agentX")).toBeInTheDocument();
  });

  it("shows Live tab by default", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getByTestId("view-live")).toBeInTheDocument());
  });

  it("renders all 7 primary tabs", () => {
    render(<App />);
    for (const label of ["Live", "Search", "Tools", "Watchlist", "Portfolio", "Alerts", "Settings"]) {
      expect(screen.getByRole("tab", { name: new RegExp(label) })).toBeInTheDocument();
    }
  });

  it("switches tabs on click", async () => {
    render(<App />);
    fireEvent.click(screen.getByRole("tab", { name: /Portfolio/ }));
    await waitFor(() => expect(screen.getByTestId("view-portfolio")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("tab", { name: /Settings/ }));
    await waitFor(() => expect(screen.getByTestId("view-settings")).toBeInTheDocument());
  });

  it("toggles the exchange between NSE and BSE", () => {
    render(<App />);
    const bse = screen.getByRole("button", { name: "BSE" });
    fireEvent.click(bse);
    expect(bse).toHaveAttribute("aria-pressed", "true");
  });
});
