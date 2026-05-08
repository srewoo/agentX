import { EmptyState } from "../layout/EmptyState";

export default function WatchlistView() {
  // The real watchlist editor is owned by the settings agent. We show
  // a friendly placeholder so the tab is never blank.
  return (
    <div className="p-3">
      <EmptyState
        icon="★"
        title="Your watchlist is empty"
        body="Add a stock from Signals or Search to start tracking it here."
      />
    </div>
  );
}
