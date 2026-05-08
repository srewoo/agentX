// Local utilities for settings/alerts components.
// (`@/lib/format` referenced in the spec is not present in this repo;
//  we inline only what we need here to avoid touching shared/.)

export type Currency = "USD" | "INR";

export function formatMoney(value: number, currency: Currency): string {
  const fmt = new Intl.NumberFormat(currency === "INR" ? "en-IN" : "en-US", {
    style: "currency",
    currency,
    maximumFractionDigits: 2,
  });
  return fmt.format(value);
}

export function clamp(n: number, min: number, max: number): number {
  return Math.min(Math.max(n, min), max);
}

export function isValidUrl(url: string): boolean {
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:";
  } catch {
    return false;
  }
}

export function isValidEmail(email: string): boolean {
  // Pragmatic, not RFC-perfect.
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim());
}

// +91 default; allow 10 digits after country code
export function isValidIndianPhone(phone: string): boolean {
  const digits = phone.replace(/\D/g, "");
  if (digits.startsWith("91")) return digits.length === 12;
  return digits.length === 10;
}

// Host pattern: bare host, *.host, or host with optional path glob
export function isValidHostPattern(pattern: string): boolean {
  const trimmed = pattern.trim();
  if (!trimmed) return false;
  // strip leading *. and any trailing /*
  const host = trimmed
    .replace(/^\*\./, "")
    .replace(/\/\*$/, "")
    .replace(/\/$/, "");
  return /^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$/i.test(
    host,
  );
}

// Mask a saved-secret indicator without ever revealing the secret.
export function savedKeyHint(charCount?: number | null): string {
  if (!charCount || charCount <= 0) return "Not configured";
  return `Configured · ${charCount} chars`;
}
