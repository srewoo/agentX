// Wraps the full Settings page (API base URL, scan interval, signal toggles,
// theme, LLM usage). The new shell intentionally keeps Settings as a tab,
// but the half-finished stub that lived here previously hid the controls
// users rely on, so we delegate to the battle-tested page implementation.
export { default } from "../pages/Settings";
