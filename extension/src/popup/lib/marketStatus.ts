import type { MarketStatus } from "./types";

/**
 * NSE/BSE regular hours: 09:15–15:30 IST.
 * Pre-open auction:        09:00–09:15 IST.
 * Post-close session:      15:40–16:00 IST.
 *
 * Returns the current market status against IST regardless of host TZ.
 */
export function getMarketStatus(now: Date = new Date()): MarketStatus {
  // Convert to IST minutes-since-midnight.
  // IST = UTC+5:30. We avoid Date.toLocaleString parsing for determinism.
  const utcMinutes = now.getUTCHours() * 60 + now.getUTCMinutes();
  const istMinutes = (utcMinutes + 5 * 60 + 30) % (24 * 60);

  // Day-of-week in IST. We use UTC day shifted by 330 minutes.
  const istDayShift = utcMinutes + 5 * 60 + 30 >= 24 * 60 ? 1 : 0;
  const istDay = (now.getUTCDay() + istDayShift) % 7;

  // Sat (6) & Sun (0) — closed all day.
  if (istDay === 0 || istDay === 6) return "CLOSED";

  const PRE_OPEN_START = 9 * 60;       // 09:00
  const OPEN_START = 9 * 60 + 15;      // 09:15
  const OPEN_END = 15 * 60 + 30;       // 15:30
  const POST_CLOSE_END = 16 * 60;      // 16:00

  if (istMinutes >= PRE_OPEN_START && istMinutes < OPEN_START) return "PRE_OPEN";
  if (istMinutes >= OPEN_START && istMinutes < OPEN_END) return "OPEN";
  if (istMinutes >= OPEN_END && istMinutes < POST_CLOSE_END) return "POST_CLOSE";
  return "CLOSED";
}

export function marketStatusLabel(s: MarketStatus): string {
  switch (s) {
    case "PRE_OPEN": return "PRE-OPEN";
    case "OPEN": return "OPEN";
    case "POST_CLOSE": return "POST-CLOSE";
    case "CLOSED": return "CLOSED";
  }
}
