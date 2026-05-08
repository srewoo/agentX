// ApiKeyForm.test.tsx
// ---------------------------------------------------------------------------
// Locks in the security-critical invariant: the form never reflects a stored
// API key value back to the DOM, regardless of how the parent passes status.

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ApiKeyForm, {
  type ApiKeyProvider,
  type ApiKeyStatus,
} from "./ApiKeyForm";

const PROVIDERS: ApiKeyProvider[] = ["openai", "gemini", "claude"];

function makeStatuses(
  overrides: Partial<Record<ApiKeyProvider, ApiKeyStatus>> = {},
): Record<ApiKeyProvider, ApiKeyStatus> {
  const base: Record<ApiKeyProvider, ApiKeyStatus> = {
    openai: { configured: false },
    gemini: { configured: false },
    claude: { configured: false },
  };
  return { ...base, ...overrides };
}

describe("ApiKeyForm", () => {
  it("never renders a stored key value in any input", () => {
    // Imagine the parent accidentally tried to send the actual key.
    // Our component's API doesn't expose a slot for it — but verify defensively
    // by rendering with `configured: true` and a high charCount.
    const statuses = makeStatuses({
      openai: { configured: true, charCount: 51 },
      claude: { configured: true, charCount: 108 },
    });
    render(
      <ApiKeyForm
        statuses={statuses}
        onSave={vi.fn()}
        onTest={vi.fn(async () => ({ ok: true, message: "ok" }))}
      />,
    );

    // Each provider's input is empty by default.
    for (const p of PROVIDERS) {
      const labelText = new RegExp(`API key$`, "i");
      const inputs = screen.getAllByLabelText(labelText);
      // Some text-fields share the same label; verify all are empty.
      for (const input of inputs) {
        expect((input as HTMLInputElement).value).toBe("");
      }
      void p; // narrow suppressions
    }

    // No element in the document contains the placeholder secret text.
    // (Catch-all: ensure no rendered text leaks anything resembling a key.)
    const all = document.body.textContent ?? "";
    expect(all).not.toMatch(/sk-[A-Za-z0-9]{10,}/);
  });

  it("shows 'Configured ✓ (NN chars)' indicator when a key is saved", () => {
    render(
      <ApiKeyForm
        statuses={makeStatuses({
          openai: { configured: true, charCount: 51 },
        })}
        onSave={vi.fn()}
        onTest={vi.fn(async () => ({ ok: true, message: "ok" }))}
      />,
    );
    // Status pill
    expect(
      screen.getAllByLabelText("API key configured").length,
    ).toBeGreaterThan(0);
    // Placeholder advertises the saved char count as a "Configured · NN chars" hint.
    const input = screen.getByLabelText(/OpenAI API key/i) as HTMLInputElement;
    expect(input.placeholder).toMatch(/Configured/);
    expect(input.placeholder).toMatch(/51 chars/);
    // And the visible hint reminds the user that blanks won't overwrite.
    expect(
      screen.getByText(/Leave blank to keep existing key/i),
    ).toBeInTheDocument();
  });

  it("uses a password input by default and toggles visibility only for typed text", async () => {
    const user = userEvent.setup();
    render(
      <ApiKeyForm
        statuses={makeStatuses()}
        onSave={vi.fn()}
        onTest={vi.fn(async () => ({ ok: true, message: "ok" }))}
      />,
    );
    // type=password → not technically a "textbox" role; query by label.
    const input = screen.getByLabelText(/OpenAI API key/i) as HTMLInputElement;
    expect(input.type).toBe("password");

    // Show button is disabled while empty.
    const showBtn = screen.getAllByRole("button", { name: /show entered key/i })[0];
    expect(showBtn).toBeDisabled();

    await user.type(input, "test-key-abc");
    expect(showBtn).not.toBeDisabled();
    await user.click(showBtn);
    expect(input.type).toBe("text");
  });

  it("calls onSave with only the newly-typed key", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(
      <ApiKeyForm
        statuses={makeStatuses({
          openai: { configured: true, charCount: 51 },
        })}
        onSave={onSave}
        onTest={vi.fn(async () => ({ ok: true, message: "ok" }))}
      />,
    );
    const input = screen.getByLabelText(/OpenAI API key/i);
    await user.type(input, "  fresh-key-xyz  ");
    const saveBtns = screen.getAllByRole("button", { name: /^save$/i });
    await user.click(saveBtns[0]);
    expect(onSave).toHaveBeenCalledWith({
      provider: "openai",
      newKey: "fresh-key-xyz",
    });
  });

  it("disables Save when the input is empty (cannot blank-overwrite a saved key)", () => {
    render(
      <ApiKeyForm
        statuses={makeStatuses({
          openai: { configured: true, charCount: 51 },
        })}
        onSave={vi.fn()}
        onTest={vi.fn(async () => ({ ok: true, message: "ok" }))}
      />,
    );
    const saveBtns = screen.getAllByRole("button", { name: /^save$/i });
    saveBtns.forEach((b) => expect(b).toBeDisabled());
  });
});
