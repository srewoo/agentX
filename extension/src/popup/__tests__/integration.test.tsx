/**
 * Popup integration test — render <App />, navigate every tab, and assert
 * each tab mounts the expected view component. Network is mocked at the
 * fetch boundary so no real HTTP is issued.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// Mock all lazy-loaded views so we don't pull heavy dependencies.
vi.mock("../pages/Dashboard", () => ({
  default: () => <div data-testid="view-live">LiveView</div>,
}));
vi.mock("../pages/Tools", () => ({
  default: () => <div data-testid="view-tools">ToolsView</div>,
}));
vi.mock("../pages/Search", () => ({
  default: () => <div data-testid="view-search">SearchView</div>,
}));
vi.mock("../pages/Watchlist", () => ({
  default: () => <div data-testid="view-watchlist">WatchlistView</div>,
}));
vi.mock("../views/PortfolioView", () => ({
  default: () => <div data-testid="view-portfolio">PortfolioView</div>,
}));
vi.mock("../pages/Alerts", () => ({
  default: () => <div data-testid="view-alerts">AlertsView</div>,
}));
vi.mock("../views/SettingsView", () => ({
  default: () => <div data-testid="view-settings">SettingsView</div>,
}));

// Mock Onboarding so we don't need to interact with it.
vi.mock("../components/Onboarding", () => ({
  default: ({ onDone }: { onDone: () => void }) => (
    <button data-testid="onboarding-done" onClick={onDone}>
      done
    </button>
  ),
}));

import App from "../App";

beforeEach(() => {
  // Stub fetch so any incidental API calls don't blow up.
  globalThis.fetch = vi.fn(async () =>
    new Response(JSON.stringify({ data: [] }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  ) as unknown as typeof fetch;
});

describe("Popup integration", () => {
  it("renders default tab (live) on first paint", async () => {
    render(<App />);
    await waitFor(() => {
      expect(screen.getByTestId("view-live")).toBeInTheDocument();
    });
  });

  it("navigates through every tab and renders the matching view", async () => {
    render(<App />);

    await waitFor(() => screen.getByTestId("view-live"));

    const tabs: Array<{ label: RegExp; testId: string }> = [
      { label: /^Search$/i, testId: "view-search" },
      { label: /^Tools$/i, testId: "view-tools" },
      { label: /^Watchlist$/i, testId: "view-watchlist" },
      { label: /^Portfolio$/i, testId: "view-portfolio" },
      { label: /^Alerts$/i, testId: "view-alerts" },
      { label: /^Settings$/i, testId: "view-settings" },
      { label: /^Live$/i, testId: "view-live" },
    ];

    for (const t of tabs) {
      const matches = screen.queryAllByText(t.label);
      expect(matches.length).toBeGreaterThan(0);
      fireEvent.click(matches[0]);
      await waitFor(() => {
        expect(screen.getByTestId(t.testId)).toBeInTheDocument();
      });
    }
  });

  it("renders all 7 navigation labels", async () => {
    render(<App />);
    await waitFor(() => screen.getByTestId("view-live"));
    const expected = ["Live", "Search", "Tools", "Watchlist", "Portfolio", "Alerts", "Settings"];
    for (const label of expected) {
      expect(screen.getAllByText(new RegExp(`^${label}$`, "i")).length).toBeGreaterThan(0);
    }
  });

  it("does not crash when fetch rejects (negative path)", async () => {
    globalThis.fetch = vi.fn(async () => {
      throw new Error("boom");
    }) as unknown as typeof fetch;
    expect(() => render(<App />)).not.toThrow();
    await waitFor(() => screen.getByTestId("view-live"));
  });

  it("handles repeated tab clicks without losing state (edge: idempotency)", async () => {
    render(<App />);
    await waitFor(() => screen.getByTestId("view-live"));
    const watchlist = screen.getAllByText(/^Watchlist$/i)[0];
    fireEvent.click(watchlist);
    fireEvent.click(watchlist);
    fireEvent.click(watchlist);
    await waitFor(() => {
      expect(screen.getByTestId("view-watchlist")).toBeInTheDocument();
    });
  });
});
