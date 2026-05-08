// Shared alert types used by AlertsManager / AlertCard / CreateAlertDialog.

export type AlertConditionKind =
  | "price_above"
  | "price_below"
  | "pct_change_1d_above"
  | "recommendation_conviction_above"
  | "volume_spike_above"
  | "breakout_N_day_high";

export type AlertChannel = "telegram" | "email" | "whatsapp" | "sms";

// Discriminated union — each kind carries exactly the params it needs.
// This makes AlertConditionBuilder safe and CreateAlertDialog validation
// straightforward.
export type AlertCondition =
  | { kind: "price_above"; threshold: number }
  | { kind: "price_below"; threshold: number }
  | { kind: "pct_change_1d_above"; pct: number }
  | { kind: "recommendation_conviction_above"; conviction: number }
  | { kind: "volume_spike_above"; multiple: number }
  | { kind: "breakout_N_day_high"; days: number };

export interface AlertChannelDelivery {
  channel: AlertChannel;
  status: "ok" | "failed";
  at: string; // ISO
}

export interface Alert {
  id: string;
  symbol: string;
  condition: AlertCondition;
  channels: AlertChannel[];
  enabled: boolean;
  note?: string | null;
  createdAt: string;
  // Most recent delivery per channel — drives the per-channel status indicator.
  lastDeliveries: AlertChannelDelivery[];
}

export interface AlertDraft {
  symbol: string;
  condition: AlertCondition;
  channels: AlertChannel[];
  note?: string;
}
