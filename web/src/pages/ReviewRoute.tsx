import { useEffect, useState } from "react";
import { Link } from "../components/Link";
import { errorMessage } from "../components/Thread";
import { api } from "../lib/api";
import type { ReviewView } from "../lib/types";
import { PrReviewPage } from "./PrReviewPage";
import { ReviewPage } from "./ReviewPage";

export function ReviewRoute({ reviewId }: { reviewId: string }) {
  const [view, setView] = useState<ReviewView | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.view(reviewId).then(setView, (e: unknown) => setError(errorMessage(e)));
  }, [reviewId]);

  if (error !== null) {
    return (
      <div className="empty-state">
        <p>Could not load this review: {error}</p>
        <Link to="/">All reviews</Link>
      </div>
    );
  }
  if (view === null) return <div className="empty-state muted">Loading…</div>;
  if (view.kind === "pr") return <PrReviewPage reviewId={reviewId} />;
  return <ReviewPage reviewId={reviewId} initialView={view} />;
}
