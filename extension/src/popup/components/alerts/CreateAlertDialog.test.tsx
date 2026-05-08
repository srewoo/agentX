// CreateAlertDialog.test.tsx
// ---------------------------------------------------------------------------
// Verifies form validation behaviour. Asserts via accessible roles + names
// (no querySelector / data-testid).

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import CreateAlertDialog from "./CreateAlertDialog";
import { validateCondition } from "./AlertConditionBuilder";
import type { AlertChannel } from "./_types";

const ENABLED_CHANNELS: AlertChannel[] = ["telegram", "email"];

describe("CreateAlertDialog — validation", () => {
  it("blocks submit when symbol is empty and shows an inline error", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn();
    render(
      <CreateAlertDialog
        open
        onClose={vi.fn()}
        onCreate={onCreate}
        enabledChannels={ENABLED_CHANNELS}
      />,
    );
    // Submit without filling symbol.
    await user.click(screen.getByRole("button", { name: /create alert/i }));
    expect(onCreate).not.toHaveBeenCalled();
    expect(await screen.findByText(/symbol is required/i)).toBeInTheDocument();
  });

  it("rejects bad symbol characters", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn();
    render(
      <CreateAlertDialog
        open
        onClose={vi.fn()}
        onCreate={onCreate}
        enabledChannels={ENABLED_CHANNELS}
      />,
    );
    await user.type(screen.getByLabelText(/symbol \(NSE\)/i), "BAD SYMBOL!");
    await user.click(screen.getByRole("button", { name: /create alert/i }));
    expect(onCreate).not.toHaveBeenCalled();
    expect(
      await screen.findByText(/use letters, digits/i),
    ).toBeInTheDocument();
  });

  it("rejects non-positive price threshold", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn();
    render(
      <CreateAlertDialog
        open
        onClose={vi.fn()}
        onCreate={onCreate}
        enabledChannels={ENABLED_CHANNELS}
      />,
    );
    await user.type(screen.getByLabelText(/symbol \(NSE\)/i), "RELIANCE");
    // Default kind is price_above, threshold = 0 → must fail.
    await user.click(screen.getByRole("button", { name: /create alert/i }));
    expect(onCreate).not.toHaveBeenCalled();
    expect(
      await screen.findByText(/threshold must be a positive number/i),
    ).toBeInTheDocument();
  });

  it("requires at least one channel", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn();
    render(
      <CreateAlertDialog
        open
        onClose={vi.fn()}
        onCreate={onCreate}
        // Empty enabled channels is valid input (parent permitted opening),
        // but forces the user to fail the channel constraint.
        enabledChannels={[]}
      />,
    );
    await user.type(screen.getByLabelText(/symbol \(NSE\)/i), "TCS");
    // Set a positive threshold so condition error doesn't shadow channel error.
    await user.clear(screen.getByLabelText(/threshold price/i));
    await user.type(screen.getByLabelText(/threshold price/i), "100");
    await user.click(screen.getByRole("button", { name: /create alert/i }));
    expect(onCreate).not.toHaveBeenCalled();
    expect(
      await screen.findByText(/pick at least one notification channel/i),
    ).toBeInTheDocument();
  });

  it("submits a valid draft and closes", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn().mockResolvedValue({
      id: "1",
      symbol: "RELIANCE",
      condition: { kind: "price_above", threshold: 2900 },
      channels: ["telegram"],
      enabled: true,
      createdAt: new Date().toISOString(),
      lastDeliveries: [],
    });
    const onClose = vi.fn();
    render(
      <CreateAlertDialog
        open
        onClose={onClose}
        onCreate={onCreate}
        enabledChannels={ENABLED_CHANNELS}
      />,
    );
    await user.type(screen.getByLabelText(/symbol \(NSE\)/i), "RELIANCE");
    const threshold = screen.getByLabelText(/threshold price/i);
    await user.clear(threshold);
    await user.type(threshold, "2900");
    // Telegram is pre-selected (initial channels = enabled.slice(0,1)) — submit.
    await user.click(screen.getByRole("button", { name: /create alert/i }));

    expect(onCreate).toHaveBeenCalledTimes(1);
    const draft = onCreate.mock.calls[0][0];
    expect(draft.symbol).toBe("RELIANCE");
    expect(draft.condition).toEqual({ kind: "price_above", threshold: 2900 });
    expect(draft.channels).toContain("telegram");
    // Dialog is closed by parent on success.
    expect(onClose).toHaveBeenCalled();
  });

  it("live preview reflects current symbol and condition", async () => {
    const user = userEvent.setup();
    render(
      <CreateAlertDialog
        open
        onClose={vi.fn()}
        onCreate={vi.fn()}
        enabledChannels={ENABLED_CHANNELS}
      />,
    );
    await user.type(screen.getByLabelText(/symbol \(NSE\)/i), "TCS");
    const threshold = screen.getByLabelText(/threshold price/i);
    await user.clear(threshold);
    await user.type(threshold, "4000");
    expect(
      screen.getByText(/Alert when TCS crosses above ₹4,000 today/i),
    ).toBeInTheDocument();
  });
});

describe("validateCondition (pure)", () => {
  it("flags zero / negative price thresholds", () => {
    expect(validateCondition({ kind: "price_above", threshold: 0 })).toMatch(
      /positive/,
    );
    expect(validateCondition({ kind: "price_below", threshold: -1 })).toMatch(
      /positive/,
    );
  });

  it("accepts a positive price threshold", () => {
    expect(
      validateCondition({ kind: "price_above", threshold: 100 }),
    ).toBeNull();
  });

  it("validates conviction range 0–10 (whole numbers)", () => {
    expect(
      validateCondition({
        kind: "recommendation_conviction_above",
        conviction: 11,
      }),
    ).toMatch(/0 and 10/);
    expect(
      validateCondition({
        kind: "recommendation_conviction_above",
        conviction: 7,
      }),
    ).toBeNull();
  });

  it("validates breakout days range", () => {
    expect(
      validateCondition({ kind: "breakout_N_day_high", days: 1 }),
    ).toMatch(/2 and 252/);
    expect(
      validateCondition({ kind: "breakout_N_day_high", days: 20 }),
    ).toBeNull();
  });
});
