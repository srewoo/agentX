import { describe, it, expect } from "vitest";
import {
  SIGNAL_TYPE_LABELS,
  SIGNAL_TIMEFRAME,
  DIRECTION_ACTION,
  ACTION_COLORS,
  getSignalTimeframe,
  LLM_MODELS,
} from "../shared/constants";

describe("constants", () => {
  describe("SIGNAL_TYPE_LABELS", () => {
    it("should have a label for every signal type", () => {
      const expectedTypes = [
        "price_spike",
        "volume_spike",
        "breakout",
        "rsi_extreme",
        "macd_crossover",
        "double_bottom",
        "bullish_engulfing",
        "hammer",
        "gap_up",
      ];
      for (const t of expectedTypes) {
        expect(SIGNAL_TYPE_LABELS[t]).toBeDefined();
        expect(typeof SIGNAL_TYPE_LABELS[t]).toBe("string");
      }
    });
  });

  describe("DIRECTION_ACTION", () => {
    it("should map directions to actions", () => {
      expect(DIRECTION_ACTION.bullish).toBe("BUY");
      expect(DIRECTION_ACTION.bearish).toBe("SELL");
      expect(DIRECTION_ACTION.neutral).toBe("HOLD");
    });
  });

  describe("ACTION_COLORS", () => {
    it("should have hex colors for all actions", () => {
      expect(ACTION_COLORS.BUY).toMatch(/^#[0-9A-Fa-f]{6}$/);
      expect(ACTION_COLORS.SELL).toMatch(/^#[0-9A-Fa-f]{6}$/);
      expect(ACTION_COLORS.HOLD).toMatch(/^#[0-9A-Fa-f]{6}$/);
    });
  });

  describe("getSignalTimeframe", () => {
    it("should return default timeframe from SIGNAL_TIMEFRAME map", () => {
      expect(getSignalTimeframe("price_spike", 5)).toBe("Intraday");
      expect(getSignalTimeframe("rsi_extreme", 5)).toBe("Swing");
      expect(getSignalTimeframe("cup_and_handle", 5)).toBe("Long-term");
    });

    it("should promote breakout to Long-term when strength >= 7", () => {
      expect(getSignalTimeframe("breakout", 6)).toBe("Swing");
      expect(getSignalTimeframe("breakout", 7)).toBe("Long-term");
      expect(getSignalTimeframe("breakout", 10)).toBe("Long-term");
    });

    it("should default to Swing for unknown signal types", () => {
      expect(getSignalTimeframe("unknown_signal", 5)).toBe("Swing");
    });
  });

  describe("LLM_MODELS", () => {
    it("should have models for all three providers", () => {
      expect(LLM_MODELS.gemini.length).toBeGreaterThan(0);
      expect(LLM_MODELS.openai.length).toBeGreaterThan(0);
      expect(LLM_MODELS.claude.length).toBeGreaterThan(0);
    });
  });

  describe("SIGNAL_TIMEFRAME", () => {
    it("should categorize intraday signals correctly", () => {
      const intradayTypes = ["price_spike", "volume_spike", "price_alert", "hammer", "gap_up", "gap_down"];
      for (const t of intradayTypes) {
        expect(SIGNAL_TIMEFRAME[t]).toBe("Intraday");
      }
    });

    it("should categorize long-term signals correctly", () => {
      const longTermTypes = ["cup_and_handle", "52_week_high", "52_week_low"];
      for (const t of longTermTypes) {
        expect(SIGNAL_TIMEFRAME[t]).toBe("Long-term");
      }
    });
  });
});
