interface SkeletonProps {
  className?: string;
  height?: number | string;
  width?: number | string;
  rounded?: "sm" | "md" | "full";
}

export function Skeleton({ className = "", height = 16, width = "100%", rounded = "md" }: SkeletonProps) {
  const radius =
    rounded === "full" ? "9999px" : rounded === "sm" ? "var(--radius-sm)" : "var(--radius-md)";
  return (
    <div
      aria-hidden="true"
      className={`animate-pulse ${className}`}
      style={{
        height,
        width,
        borderRadius: radius,
        background: "var(--bg-panel-hover)",
      }}
    />
  );
}

export function CardSkeleton() {
  return (
    <div
      className="p-3 rounded-lg space-y-2 border"
      style={{ background: "var(--bg-panel)", borderColor: "var(--border-default)" }}
    >
      <Skeleton width="60%" />
      <Skeleton width="100%" height={28} />
      <div className="flex gap-2">
        <Skeleton width="30%" />
        <Skeleton width="30%" />
      </div>
    </div>
  );
}
