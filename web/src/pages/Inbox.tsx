import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Link } from "../components/Link";
import { errorMessage } from "../components/Thread";
import { api } from "../lib/api";
import { useElapsedSeconds } from "../lib/hooks";
import { filterItems, prLabel, recentLabel } from "../lib/inbox";
import { navigate, reviewPath } from "../lib/router";
import { formatAge } from "../lib/time";
import type { Inbox as InboxData, InboxItem, ReviewSummary } from "../lib/types";

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

function InboxRow({
  item,
  disabled,
  onOpen,
}: {
  item: InboxItem;
  disabled: boolean;
  onOpen(ref: string): void;
}) {
  const content = (
    <>
      <span className="pr-ref">{prLabel(item.repo, item.number)}</span>
      <span className="inbox-title">{item.title}</span>
      {item.is_draft && <span className="tag">Draft</span>}
      {item.review_id && <span className="tag tag-opened">Opened</span>}
      <span className="muted inbox-author">{item.author ?? "unknown"}</span>
      <time className="muted inbox-time" dateTime={item.updated_at} title={item.updated_at}>
        {formatAge(item.updated_at)}
      </time>
    </>
  );
  return (
    <li>
      {item.review_id ? (
        <Link to={reviewPath(item.review_id)} className="inbox-row">
          {content}
        </Link>
      ) : (
        <button
          type="button"
          className="inbox-row"
          disabled={disabled}
          onClick={() => onOpen(item.url)}
        >
          {content}
        </button>
      )}
    </li>
  );
}

function ItemList({
  items,
  disabled,
  onOpen,
  empty,
}: {
  items: readonly InboxItem[];
  disabled: boolean;
  onOpen(ref: string): void;
  empty: string;
}) {
  if (items.length === 0) return <p className="muted list-empty">{empty}</p>;
  return (
    <ul className="review-list">
      {items.map((item) => (
        <InboxRow
          key={`${item.repo}#${item.number}`}
          item={item}
          disabled={disabled}
          onOpen={onOpen}
        />
      ))}
    </ul>
  );
}

function TeamRequests({
  items,
  disabled,
  onOpen,
}: {
  items: readonly InboxItem[];
  disabled: boolean;
  onOpen(ref: string): void;
}) {
  const [query, setQuery] = useState("");
  const shown = filterItems(items, query);
  return (
    <details className="inbox-section team-section">
      <summary>
        <h3>Requested from your teams</h3>
        <span className="count">
          {query.trim() ? `${shown.length} of ${items.length}` : items.length}
        </span>
      </summary>
      <input
        type="search"
        className="team-filter"
        placeholder="Filter by repo, #number, title, or author"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      />
      <ItemList
        items={shown}
        disabled={disabled}
        onOpen={onOpen}
        empty={items.length === 0 ? "No team review requests." : "No requests match the filter."}
      />
    </details>
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
  const [inbox, setInbox] = useState<InboxData | null>(null);
  const [inboxError, setInboxError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [reviews, setReviews] = useState<ReviewSummary[] | null>(null);
  const [reviewsError, setReviewsError] = useState<string | null>(null);
  const [ref, setRef] = useState("");
  const opener = usePrOpener();

  const loadInbox = useCallback(async (refresh: boolean) => {
    setRefreshing(true);
    try {
      setInbox(await api.inbox(refresh));
      setInboxError(null);
    } catch (e) {
      setInboxError(errorMessage(e));
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    document.title = "Code Review";
    void loadInbox(false);
    api.listReviews().then(
      (list) => setReviews([...list].sort(newestFirst)),
      (e: unknown) => setReviewsError(errorMessage(e)),
    );
  }, [loadInbox]);

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
            <h2>Review requests</h2>
            {inbox && (
              <span className="muted" title={inbox.fetched_at}>
                updated {formatAge(inbox.fetched_at)}
              </span>
            )}
            <span className="spacer" />
            <button
              type="button"
              className="button"
              disabled={refreshing}
              onClick={() => void loadInbox(true)}
            >
              {refreshing ? "Refreshing…" : "Refresh"}
            </button>
          </div>
          {inboxError && <p className="form-error">Could not load review requests: {inboxError}</p>}
          {inbox === null && !inboxError && <p className="muted">Loading review requests…</p>}
          {inbox && (
            <>
              <div className="inbox-section direct-section">
                <div className="section-header">
                  <h3>Requested from you</h3>
                  <span className="count">{inbox.direct.length}</span>
                </div>
                <ItemList
                  items={inbox.direct}
                  disabled={opening}
                  onOpen={onOpen}
                  empty="Nothing requests your review directly."
                />
              </div>
              <TeamRequests items={inbox.team} disabled={opening} onOpen={onOpen} />
            </>
          )}
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
