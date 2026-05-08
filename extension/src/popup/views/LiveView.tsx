import { EmptyState } from "../layout/EmptyState";

/**
 * "Live" tab — streaming quotes & live chart for the user's primary symbol.
 * The real chart lives in the `charts` agent's `LiveChart`. Until then we
 * render a friendly empty state so the popup is never broken.
 */
export default function LiveView() {
  return (
    <div className="p-3">
      <EmptyState
        icon="◉"
        title="Live market view"
        body="Pick a stock from your watchlist or signals to stream a live chart with depth and indicators."
      />
    </div>
  );
}
