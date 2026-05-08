import { EmptyState } from "../layout/EmptyState";

export default function AlertsView() {
  return (
    <div className="p-3">
      <EmptyState
        icon="🔔"
        title="No alerts yet"
        body="Set price or signal alerts to be pinged when the market hits your level."
      />
    </div>
  );
}
