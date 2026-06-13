interface LoadingStateProps {
  label?: string;
}

// Watercolor-square wave used for the initial page load and in-card loading.
export function LoadingState({ label = "Loading" }: LoadingStateProps) {
  return (
    <div className="loading-state" role="status" aria-label={label}>
      <span className="loading-state__wave" aria-hidden="true">
        <span />
        <span />
        <span />
        <span />
        <span />
      </span>
      <span>{label}…</span>
    </div>
  );
}
