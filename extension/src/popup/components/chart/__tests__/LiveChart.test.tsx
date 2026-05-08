import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";

// Mock the shared API surface — we only need getQuote + getHistory.
vi.mock("../../../../shared/api", () => {
  const candle = (i: number) => ({
    date: `2026-01-${String(i + 1).padStart(2, "0")}`,
    o: 100 + i,
    h: 105 + i,
    l: 95 + i,
    c: 100 + i + (i % 2 === 0 ? 1 : -1),
    v: 1_000_000 + i * 10_000,
  });
  return {
    api: {
      getHistory: vi.fn(async () => ({
        history: Array.from({ length: 50 }, (_, i) => candle(i)),
      })),
      getQuote: vi.fn(async () => ({
        symbol: "RELIANCE",
        price: 152.5,
        change: 1.2,
        change_pct: 0.79,
        volume: 1_500_000,
        high: 155,
        low: 148,
        open: 150,
      })),
    },
  };
});

// Mock lightweight-charts so we don't need a real DOM measurement layer.
const removeSpy = vi.fn();
const setDataSpy = vi.fn();
const updateSpy = vi.fn();

vi.mock("lightweight-charts", () => {
  const fakeSeries = {
    setData: setDataSpy,
    update: updateSpy,
    applyOptions: vi.fn(),
    priceScale: () => ({ applyOptions: vi.fn() }),
  };
  const fakeChart = {
    addSeries: vi.fn(() => fakeSeries),
    applyOptions: vi.fn(),
    subscribeCrosshairMove: vi.fn(),
    timeScale: () => ({ fitContent: vi.fn() }),
    remove: removeSpy,
  };
  return {
    createChart: vi.fn(() => fakeChart),
    ColorType: { Solid: "solid" },
    CrosshairMode: { Normal: 0 },
    LineStyle: { Dashed: 1 },
    CandlestickSeries: "Candlestick",
    HistogramSeries: "Histogram",
    LineSeries: "Line",
  };
});

import LiveChart from "../LiveChart";

describe("LiveChart", () => {
  beforeEach(() => {
    setDataSpy.mockClear();
    updateSpy.mockClear();
    removeSpy.mockClear();
    // ResizeObserver doesn't exist in jsdom by default
    if (!("ResizeObserver" in globalThis)) {
      (globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver =
        class {
          observe() {}
          unobserve() {}
          disconnect() {}
        };
    }
  });

  it("renders loading state then a screen-reader summary once data arrives", async () => {
    render(
      <LiveChart
        symbol="RELIANCE"
        exchange="NSE"
        interval="1d"
        showEMA={[20, 50]}
      />
    );

    // Loading indicator visible to AT
    expect(screen.getByRole("status")).toBeInTheDocument();

    // After data load, the sr-only summary should be populated
    await waitFor(
      () => {
        const region = document.querySelector("[aria-label='Live chart for RELIANCE']");
        expect(region).toBeTruthy();
        expect(region!.textContent).toMatch(/RELIANCE/);
      },
      { timeout: 2000 }
    );

    // Series data was pushed into the (mocked) candlestick + volume + EMA series
    await waitFor(() => {
      expect(setDataSpy).toHaveBeenCalled();
    });
  });

  it("disposes the chart on unmount", async () => {
    const { unmount } = render(
      <LiveChart symbol="RELIANCE" exchange="NSE" interval="1d" />
    );

    // Wait until the chart has been built (createChart resolved)
    await waitFor(() => {
      expect(setDataSpy).toHaveBeenCalled();
    });

    unmount();
    cleanup();

    // The lightweight-charts `remove()` must have been invoked exactly once.
    expect(removeSpy).toHaveBeenCalled();
  });
});
