// Public surface of the settings module.
// Each component is its own file; this barrel keeps imports tidy.

export { default as SettingsPanel } from "./SettingsPanel";
export type {
  SettingsPanelData,
  SettingsPanelHandlers,
} from "./SettingsPanel";

export { default as ApiKeyForm } from "./ApiKeyForm";
export type {
  ApiKeyProvider,
  ApiKeyStatus,
  ApiKeyFormValue,
} from "./ApiKeyForm";

export { default as ChannelsForm } from "./ChannelsForm";
export type {
  ChannelKind,
  ChannelsConfig,
  ChannelsFormPatch,
  ChannelDelivery,
} from "./ChannelsForm";

export { default as SpendCapForm } from "./SpendCapForm";
export type { LlmUsageToday } from "./SpendCapForm";

export { default as WatchlistEditor } from "./WatchlistEditor";
export type { WatchlistRow } from "./WatchlistEditor";

export { default as SiteAllowlistEditor } from "./SiteAllowlistEditor";

export { default as ThemeToggle } from "./ThemeToggle";
export type { ThemePreference } from "./ThemeToggle";

export { default as BackendUrlForm } from "./BackendUrlForm";
