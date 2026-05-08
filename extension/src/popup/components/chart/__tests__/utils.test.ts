import { describe, it, expect } from "vitest";
import {
  computeEMA,
  computeRSI,
  computeMACD,
  formatINRPrecise,
  formatChangePct,
  formatVolume,
  buildA11ySummary,
  intervalToRange,
  toLineData,
} from "../utils";

describe("formatINRPrecise", () => {
  it("formats finite numbers as INR with 2 decimals", () => {
    const out = formatINRPrecise(2876.452);
    // The Intl rendering uses a non-breaking space; assert the digits/symbol.
    expect(out).toMatch(/2,876\.45/);
    expect(out).toContain("₹");
  });

  it("returns em-dash for null / NaN / undefined", () => {
    expect(formatINRPrecise(null)).toBe("—");
    expect(formatINRPrecise(undefined)).toBe("—");
    expect(formatINRPrecise(NaN)).toBe("—");
  });
});

describe("formatChangePct", () => {
  it("prefixes positive values with +", () => {
    expect(formatChangePct(2.345)).toBe("+2.35%");
  });
  it("keeps negative sign", () => {
    expect(formatChangePct(-1.2)).toBe("-1.20%");
  });
  it("handles missing data", () => {
    expect(formatChangePct(null)).toBe("—");
  });
});

describe("formatVolume", () => {
  it("scales to crore / lakh / thousand", () => {
    expect(formatVolume(1.5e7)).toBe("1.50Cr");
    expect(formatVolume(2.3e5)).toBe("2.30L");
    expect(formatVolume(4500)).toBe("4.5K");
    expect(formatVolume(120)).toBe("120");
  });
});

describe("computeEMA", () => {
  it("returns nulls until the period seed completes, then real values", () => {
    const closes = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
    const ema = computeEMA(closes, 3);
    expect(ema.slice(0, 2)).toEqual([null, null]);
    expect(ema[2]).toBeCloseTo(2);
    expect(ema[ema.length - 1]).toBeGreaterThan(ema[2]!);
  });

  it("returns all nulls when input is shorter than period", () => {
    const ema = computeEMA([1, 2], 5);
    expect(ema).toEqual([null, null]);
  });
});

describe("computeRSI", () => {
  it("returns 100 when there are no losses", () => {
    const closes = Array.from({ length: 20 }, (_, i) => i + 1);
    const rsi = computeRSI(closes, 14);
    expect(rsi[14]).toBe(100);
  });

  it("produces values within [0, 100]", () => {
    const closes = [10, 12, 11, 13, 14, 12, 11, 13, 15, 14, 16, 15, 17, 16, 18, 17];
    const rsi = computeRSI(closes, 14);
    const last = rsi[rsi.length - 1];
    expect(last).not.toBeNull();
    expect(last!).toBeGreaterThanOrEqual(0);
    expect(last!).toBeLessThanOrEqual(100);
  });
});

describe("computeMACD", () => {
  it("detects an EMA crossover by sign change in macd line", () => {
    // Fabricated downtrend then uptrend — fast EMA should cross slow EMA.
    const downtrend = Array.from({ length: 30 }, (_, i) => 100 - i * 0.5);
    const uptrend = Array.from({ length: 30 }, (_, i) => 85 + i * 0.5);
    const closes = [...downtrend, ...uptrend];
    const { macd, signal } = computeMACD(closes, 12, 26, 9);

    const cleaned = macd.map((v) => v ?? 0);
    let sawNegative = false;
    let sawCrossUp = false;
    for (let i = 26; i < cleaned.length; i++) {
      if (cleaned[i] < 0) sawNegative = true;
      if (sawNegative && cleaned[i] > 0) {
        sawCrossUp = true;
        break;
      }
    }
    expect(sawNegative).toBe(true);
    expect(sawCrossUp).toBe(true);
    // Signal line should be defined for the back half
    expect(signal[signal.length - 1]).not.toBeNull();
  });
});

describe("intervalToRange", () => {
  it("maps every supported interval", () => {
    expect(intervalToRange("1m")).toBe("1d");
    expect(intervalToRange("5m")).toBe("5d");
    expect(intervalToRange("15m")).toBe("5d");
    expect(intervalToRange("1h")).toBe("1mo");
    expect(intervalToRange("1d")).toBe("1y");
  });
});

describe("toLineData", () => {
  it("drops null values and keeps time alignment", () => {
    const times = ["a", "b", "c", "d"];
    const values = [null, 1, null, 2];
    expect(toLineData(times, values)).toEqual([
      { time: "b", value: 1 },
      { time: "d", value: 2 },
    ]);
  });
});

describe("buildA11ySummary", () => {
  it("includes direction, percent, last price, and range when available", () => {
    const s = buildA11ySummary({
      symbol: "RELIANCE",
      changePct: 2.34,
      lastPrice: 2876.45,
      rangeLow: 2790,
      rangeHigh: 2910,
      rangeDays: 5,
    });
    expect(s).toContain("RELIANCE");
    expect(s).toContain("up");
    expect(s).toContain("2.34%");
    expect(s).toContain("5-day range");
  });

  it("handles missing price gracefully", () => {
    expect(
      buildA11ySummary({
        symbol: "FOO",
        changePct: null,
        lastPrice: null,
        rangeLow: null,
        rangeHigh: null,
      })
    ).toContain("unavailable");
  });
});
