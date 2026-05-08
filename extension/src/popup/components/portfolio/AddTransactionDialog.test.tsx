import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import AddTransactionDialog, { __validateForTest as validate } from "./AddTransactionDialog";

const createTransaction = vi.fn();

vi.mock("@/lib/api", () => ({
  api: { portfolio: { createTransaction: (...args: unknown[]) => createTransaction(...args) } },
}));

beforeEach(() => {
  createTransaction.mockReset();
});

describe("AddTransactionDialog — validation rules", () => {
  it("flags missing required fields", () => {
    const errs = validate({ symbol: "", exchange: "", side: "buy", qty: "", price: "", fees: "", notes: "" });
    expect(errs.symbol).toBeDefined();
    expect(errs.exchange).toBeDefined();
    expect(errs.qty).toBeDefined();
    expect(errs.price).toBeDefined();
  });

  it("rejects non-positive qty and price", () => {
    const errs = validate({ symbol: "RELIANCE", exchange: "NSE", side: "buy", qty: "0", price: "-1", fees: "", notes: "" });
    expect(errs.qty).toMatch(/> 0/);
    expect(errs.price).toMatch(/> 0/);
  });

  it("rejects fractional qty", () => {
    const errs = validate({ symbol: "RELIANCE", exchange: "NSE", side: "buy", qty: "1.5", price: "100", fees: "", notes: "" });
    expect(errs.qty).toMatch(/Whole number/);
  });

  it("rejects negative fees but accepts blank fees", () => {
    const bad = validate({ symbol: "RELIANCE", exchange: "NSE", side: "buy", qty: "1", price: "100", fees: "-1", notes: "" });
    expect(bad.fees).toBeDefined();
    const ok = validate({ symbol: "RELIANCE", exchange: "NSE", side: "buy", qty: "1", price: "100", fees: "", notes: "" });
    expect(ok.fees).toBeUndefined();
  });

  it("rejects notes longer than 280 chars", () => {
    const errs = validate({ symbol: "RELIANCE", exchange: "NSE", side: "buy", qty: "1", price: "100", fees: "", notes: "x".repeat(281) });
    expect(errs.notes).toBeDefined();
  });

  it("accepts a valid form", () => {
    const errs = validate({ symbol: "RELIANCE", exchange: "NSE", side: "buy", qty: "10", price: "2500.5", fees: "20", notes: "ok" });
    expect(Object.keys(errs)).toHaveLength(0);
  });
});

describe("AddTransactionDialog — UI behavior", () => {
  it("disables submit while invalid and enables when valid", async () => {
    render(<AddTransactionDialog open onClose={() => {}} />);
    const submit = screen.getByRole("button", { name: /save transaction/i });
    expect(submit).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/Symbol/i), { target: { value: "RELIANCE" } });
    fireEvent.change(screen.getByLabelText(/Quantity/i), { target: { value: "10" } });
    fireEvent.change(screen.getByLabelText(/^Price/i), { target: { value: "2500" } });

    await waitFor(() => expect(submit).not.toBeDisabled());
  });

  it("calls api on valid submit and fires onSuccess", async () => {
    createTransaction.mockResolvedValueOnce({ ok: true });
    const onSuccess = vi.fn();
    const onClose = vi.fn();
    render(<AddTransactionDialog open onClose={onClose} onSuccess={onSuccess} />);

    fireEvent.change(screen.getByLabelText(/Symbol/i), { target: { value: "tcs" } });
    fireEvent.change(screen.getByLabelText(/Quantity/i), { target: { value: "5" } });
    fireEvent.change(screen.getByLabelText(/^Price/i), { target: { value: "3500" } });

    fireEvent.click(screen.getByRole("button", { name: /save transaction/i }));

    await waitFor(() => expect(createTransaction).toHaveBeenCalledTimes(1));
    expect(createTransaction.mock.calls[0][0]).toMatchObject({
      symbol: "TCS",
      qty: 5,
      price: 3500,
      side: "buy",
    });
    await waitFor(() => expect(onSuccess).toHaveBeenCalled());
    expect(onClose).toHaveBeenCalled();
  });

  it("shows inline error when api rejects", async () => {
    createTransaction.mockRejectedValueOnce(new Error("Server unreachable"));
    render(<AddTransactionDialog open onClose={() => {}} />);

    fireEvent.change(screen.getByLabelText(/Symbol/i), { target: { value: "RELIANCE" } });
    fireEvent.change(screen.getByLabelText(/Quantity/i), { target: { value: "10" } });
    fireEvent.change(screen.getByLabelText(/^Price/i), { target: { value: "2500" } });
    fireEvent.click(screen.getByRole("button", { name: /save transaction/i }));

    await screen.findByText(/Server unreachable/i);
  });

  it("closes on Escape", () => {
    const onClose = vi.fn();
    render(<AddTransactionDialog open onClose={onClose} />);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });
});
