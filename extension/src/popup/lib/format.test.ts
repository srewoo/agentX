import { describe, it, expect } from "vitest";
import {
  formatINR,
  formatINRPrecise,
  formatPct,
  formatVolume,
  formatChange,
  pctColorClass,
} from "./format";

describe("formatINR (compact)", () => {
  it("renders crore for >= 1 Cr", () => {
    expect(formatINR(123456789)).toBe("₹12.35 Cr");
    expect(formatINR(10000000)).toBe("₹1.00 Cr");
  });

  it("renders lakh for >= 1 L", () => {
    expect(formatINR(123456)).toBe("₹1.23 L");
    expect(formatINR(100000)).toBe("₹1.00 L");
  });

  it("renders thousand suffix for >= 1K", () => {
    expect(formatINR(12345)).toBe("₹12.35 K");
  });

  it("renders rupees with 2dp for small values", () => {
    expect(formatINR(99)).toBe("₹99.00");
  });

  it("handles negatives", () => {
    expect(formatINR(-12345678)).toBe("-₹1.23 Cr");
  });

  it("returns em-dash for null/undefined/NaN", () => {
    expect(formatINR(null)).toBe("—");
    expect(formatINR(undefined)).toBe("—");
    expect(formatINR(NaN)).toBe("—");
  });
});

describe("formatINRPrecise (Indian grouping)", () => {
  it("groups using lakh/crore separators", () => {
    expect(formatINRPrecise(1234.56)).toBe("₹1,234.56");
    expect(formatINRPrecise(1234567.89)).toBe("₹12,34,567.89");
    expect(formatINRPrecise(12345678.9)).toBe("₹1,23,45,678.90");
  });

  it("respects fractionDigits", () => {
    expect(formatINRPrecise(99, 0)).toBe("₹99");
  });

  it("handles negative values", () => {
    expect(formatINRPrecise(-1234.5)).toBe("-₹1,234.50");
  });

  it("returns em-dash for missing values", () => {
    expect(formatINRPrecise(null)).toBe("—");
    expect(formatINRPrecise(NaN)).toBe("—");
  });
});

describe("formatPct", () => {
  it("treats |x| <= 1 as a fraction", () => {
    expect(formatPct(0.0234)).toBe("+2.34%");
    expect(formatPct(-0.0501)).toBe("-5.01%");
  });

  it("treats |x| > 1 as already a percent", () => {
    expect(formatPct(2.34)).toBe("+2.34%");
    expect(formatPct(-12.5)).toBe("-12.50%");
  });

  it("respects fractionDigits and alwaysSign", () => {
    expect(formatPct(0.123, { fractionDigits: 1 })).toBe("+12.3%");
    expect(formatPct(0.123, { alwaysSign: false })).toBe("12.30%");
  });

  it("returns em-dash for missing values", () => {
    expect(formatPct(null)).toBe("—");
  });
});

describe("formatVolume", () => {
  it("uses lakh / crore short forms", () => {
    expect(formatVolume(1234567)).toBe("12.35L");
    expect(formatVolume(123456789)).toBe("12.35Cr");
    expect(formatVolume(2500)).toBe("2.50K");
    expect(formatVolume(900)).toBe("900");
  });
});

describe("formatChange", () => {
  it("includes a leading sign", () => {
    expect(formatChange(12.5)).toBe("+12.50");
    expect(formatChange(-3.14)).toBe("-3.14");
    expect(formatChange(0)).toBe("0.00");
  });
});

describe("pctColorClass", () => {
  it("maps sign to color", () => {
    expect(pctColorClass(0.01)).toBe("text-profit");
    expect(pctColorClass(-0.01)).toBe("text-loss");
    expect(pctColorClass(0)).toBe("text-zinc-400");
    expect(pctColorClass(null)).toBe("text-zinc-400");
  });
});
