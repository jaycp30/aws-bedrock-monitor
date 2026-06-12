import Lottie from "lottie-react";
import loadingAnimation from "../assets/loading.json";

// Skip the animation for users who prefer reduced motion (they keep the text).
const REDUCED_MOTION =
  typeof window !== "undefined" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

interface LoadingStateProps {
  label?: string;
}

// Watercolor-square wave used for the initial page load and in-card loading.
export function LoadingState({ label = "Loading" }: LoadingStateProps) {
  return (
    <div className="loading-state" role="status" aria-label={label}>
      {!REDUCED_MOTION && (
        <Lottie animationData={loadingAnimation} loop className="loading-state__anim" />
      )}
      <span>{label}…</span>
    </div>
  );
}
