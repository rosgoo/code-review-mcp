import { useEffect, useState } from "react";
import { Link } from "../components/Link";
import { errorMessage } from "../components/Thread";
import { api } from "../lib/api";
import { reviewPath } from "../lib/router";
import { formatAge } from "../lib/time";
import type { ReviewSummary } from "../lib/types";

const newestFirst = (a: ReviewSummary, b: ReviewSummary) =>
  b.created_at.localeCompare(a.created_at);

export function Inbox() {
  const [reviews, setReviews] = useState<ReviewSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    document.title = "Code Review";
    api.listReviews().then(
      (list) => setReviews([...list].sort(newestFirst)),
      (e: unknown) => setError(errorMessage(e)),
    );
  }, []);

  return (
    <div className="inbox-page">
      <header className="topbar">
        <h1 className="topbar-title">Code Review</h1>
      </header>
      <main className="inbox">
        <h2>Reviews</h2>
        {error && <p className="form-error">Could not load reviews: {error}</p>}
        {reviews === null && !error && <p className="muted">Loading…</p>}
        {reviews?.length === 0 && (
          <p className="muted">No reviews yet. An agent opens one with open_diff or show_files.</p>
        )}
        {reviews && reviews.length > 0 && (
          <ul className="review-list">
            {reviews.map((review) => (
              <li key={review.id}>
                <Link to={reviewPath(review.id)} className="review-row">
                  <span className="review-row-title">{review.title}</span>
                  <span className="tag">{review.kind}</span>
                  {review.mode && <span className="tag">{review.mode}</span>}
                  <time className="muted" dateTime={review.created_at} title={review.created_at}>
                    {formatAge(review.created_at)}
                  </time>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </main>
    </div>
  );
}
