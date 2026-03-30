import { Component, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

/**
 * React error boundary that catches render errors in its subtree.
 * Must be a class component — hooks cannot catch render-phase errors.
 *
 * Usage: wrap each tab page with key={activeTab} so the boundary resets
 * when the user navigates away and back.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error("[agentX ErrorBoundary]", error, info.componentStack);
  }

  render() {
    if (this.state.hasError) {
      return (
        this.props.fallback ?? (
          <div className="flex flex-col items-center justify-center h-full gap-3 p-6 text-center">
            <span className="text-3xl text-warn">!</span>
            <p className="text-sm font-medium text-zinc-300">Something went wrong</p>
            <p className="text-xs text-zinc-500 max-w-[260px] leading-relaxed">
              {this.state.error?.message ?? "An unexpected error occurred."}
            </p>
            <button
              onClick={() => this.setState({ hasError: false, error: null })}
              className="text-xs bg-brand/20 text-brand-light border border-brand/30 px-3 py-1 rounded hover:bg-brand/30"
            >
              Retry
            </button>
          </div>
        )
      );
    }
    return this.props.children;
  }
}
