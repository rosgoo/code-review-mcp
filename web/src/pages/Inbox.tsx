import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import {
  InboxControlsBar,
  InboxSection,
  useInboxControls,
  useInboxLists,
} from "../components/InboxLists";
import { Link } from "../components/Link";
import { errorMessage } from "../components/Thread";
import { api } from "../lib/api";
import { useElapsedSeconds } from "../lib/hooks";
import { INBOX_NAMES, authorsOf, recentLabel } from "../lib/inbox";
import { navigate, reviewPath } from "../lib/router";
import { formatAge } from "../lib/time";
import type { ReviewSummary } from "../lib/types";

const newestFirst = (a: ReviewSummary, b: ReviewSummary) =>
  b.created_at.localeCompare(a.created_at);

interface Opening {
  ref: string;
  startedAt: number;
}

function usePrOpener() {
  const [opening, setOpening] = useState<Opening | null>(null);
  const [error, setError] = useState<string | null>(null);
  const busy = useRef(false);

  const open = useCallback(async (ref: string) => {
    const target = ref.trim();
    if (!target || busy.current) return;
    busy.current = true;
    setError(null);
    setOpening({ ref: target, startedAt: Date.now() });
    try {
      const opened = await api.openPr(target);
      navigate(reviewPath(opened.review_id), { state: opened.note ? { note: opened.note } : null });
    } catch (e) {
      setError(errorMessage(e));
      setOpening(null);
      busy.current = false;
    }
  }, []);

  return { opening, error, open };
}

function OpenProgress({ opening }: { opening: Opening }) {
  const elapsed = useElapsedSeconds(opening.startedAt);
  return (
    <p className="open-progress" role="status">
      <span className="spinner" aria-hidden="true" /> Opening <code>{opening.ref}</code>… {elapsed}{" "}
      s
      <span className="muted">
        {" "}
        The first open fetches the PR and checks out a worktree. It can take 10 s or more.
      </span>
    </p>
  );
}

function RecentReviews({ reviews }: { reviews: readonly ReviewSummary[] }) {
  if (reviews.length === 0) {
    return (
      <p className="muted list-empty">
        No reviews yet. Open a PR above, or let an agent call open_diff.
      </p>
    );
  }
  return (
    <ul className="review-list">
      {reviews.map((review) => {
        const label = recentLabel(review);
        return (
          <li key={review.id}>
            <Link to={reviewPath(review.id)} className="review-row">
              <span className={review.kind === "pr" ? "pr-ref" : "review-row-title"}>
                {label.primary}
              </span>
              {label.secondary && <span className="review-row-title">{label.secondary}</span>}
              {review.kind === "pr" && review.status === "closed" && (
                <span className="tag">closed</span>
              )}
              <span className="tag">{review.kind}</span>
              {review.kind === "local" && review.mode && <span className="tag">{review.mode}</span>}
              <time className="muted" dateTime={review.created_at} title={review.created_at}>
                {formatAge(review.created_at)}
              </time>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

export function Inbox() {
  const { states, load, refreshAll } = useInboxLists();
  const [refreshing, setRefreshing] = useState(false);
  const [reviews, setReviews] = useState<ReviewSummary[] | null>(null);
  const [reviewsError, setReviewsError] = useState<string | null>(null);
  const [ref, setRef] = useState("");
  const [controls, setControls] = useInboxControls();
  const opener = usePrOpener();
  const authors = useMemo(
    () => authorsOf(INBOX_NAMES.map((name) => states[name].list?.items ?? [])),
    [states],
  );

  useEffect(() => {
    document.title = "Code Review";
    api.listReviews().then(
      (list) => setReviews([...list].sort(newestFirst)),
      (e: unknown) => setReviewsError(errorMessage(e)),
    );
  }, []);

  async function refreshInbox() {
    setRefreshing(true);
    await refreshAll();
    setRefreshing(false);
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void opener.open(ref);
  }

  const opening = opener.opening !== null;
  const onOpen = (target: string) => void opener.open(target);

  return (
    <div className="inbox-page">
      <header className="topbar">
        <h1 className="topbar-title">Code Review</h1>
      </header>
      <main className="inbox">
        <form className="open-form" onSubmit={onSubmit}>
          <input
            type="text"
            className="open-input"
            aria-label="PR to open"
            placeholder="Open a PR: URL, owner/repo#123, #123, commit SHA, or branch"
            value={ref}
            disabled={opening}
            onChange={(event) => setRef(event.target.value)}
          />
          <button type="submit" className="button primary" disabled={opening || !ref.trim()}>
            Open
          </button>
        </form>
        {opener.opening && <OpenProgress opening={opener.opening} />}
        {opener.error && <p className="form-error">Could not open the PR: {opener.error}</p>}

        <section className="inbox-section">
          <div className="section-header">
            <h2>Pull requests</h2>
            <span className="spacer" />
            <button
              type="button"
              className="button"
              disabled={refreshing}
              onClick={() => void refreshInbox()}
            >
              {refreshing ? "Refreshing…" : "Refresh"}
            </button>
          </div>
          <InboxControlsBar controls={controls} authors={authors} onChange={setControls} />
          <InboxSection
            title="Requested from you"
            className="direct-section"
            state={states.direct}
            controls={controls}
            disabled={opening}
            onOpen={onOpen}
            onRetry={() => void load("direct")}
            empty="Nothing requests your review directly."
          />
          <InboxSection
            title="Your PRs"
            className="mine-section"
            state={states.mine}
            controls={controls}
            disabled={opening}
            onOpen={onOpen}
            onRetry={() => void load("mine")}
            empty="You have no open PRs."
          />
          <InboxSection
            title="Requested from your teams"
            className="team-section"
            state={states.team}
            controls={controls}
            disabled={opening}
            onOpen={onOpen}
            onRetry={() => void load("team")}
            collapsible
            empty="No team review requests."
          />
        </section>

        <section className="inbox-section">
          <div className="section-header">
            <h2>Recent reviews</h2>
          </div>
          {reviewsError && <p className="form-error">Could not load reviews: {reviewsError}</p>}
          {reviews === null && !reviewsError && <p className="muted">Loading…</p>}
          {reviews && <RecentReviews reviews={reviews} />}
        </section>
      </main>
    </div>
  );
}
