import { useEffect, useState } from "react";
import { ApiError, api } from "../lib/api";
import {
  confirmContent,
  eventButtons,
  staleBlockLabel,
  staleIdsFrom,
  threadLocation,
  type ConfirmContent,
} from "../lib/review";
import type { PrView, ReviewEventName, ReviewThread, SubmitReviewResult } from "../lib/types";
import { Markdown } from "./Markdown";
import { errorMessage } from "./Thread";

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

function ConfirmDialog({
  content,
  busy,
  error,
  onConfirm,
  onCancel,
}: {
  content: ConfirmContent;
  busy: boolean;
  error: string | null;
  onConfirm(): void;
  onCancel(): void;
}) {
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && !busy) onCancel();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [busy, onCancel]);

  return (
    <div className="modal-backdrop">
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
        <h2 id="confirm-title">Submit review: {content.eventLabel}</h2>
        <p className="muted">This posts to GitHub. It cannot be undone from here.</p>
        <h3>Summary</h3>
        {content.summary ? (
          <Markdown text={content.summary} className="confirm-summary" />
        ) : (
          <p className="muted">No summary.</p>
        )}
        <h3>{plural(content.comments.length, "comment")}</h3>
        {content.comments.length === 0 ? (
          <p className="muted">No inline comments.</p>
        ) : (
          <ul className="confirm-comments">
            {content.comments.map((comment) => (
              <li key={comment.id}>
                <code>{comment.location}</code>
                <span className="confirm-preview">{comment.preview}</span>
              </li>
            ))}
          </ul>
        )}
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div className="form-actions">
          <button type="button" className="button" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button type="button" className="button primary" onClick={onConfirm} disabled={busy}>
            {busy ? "Submitting…" : `Submit: ${content.eventLabel}`}
          </button>
        </div>
      </div>
    </div>
  );
}

interface PrReviewBarProps {
  pr: PrView;
  drafts: readonly ReviewThread[];
  stale: readonly ReviewThread[];
  onJumpToThread(thread: ReviewThread): void;
  onSubmitted(result: SubmitReviewResult, event: ReviewEventName): void;
  onStale(threadIds: readonly string[]): void;
}

export function PrReviewBar({
  pr,
  drafts,
  stale,
  onJumpToThread,
  onSubmitted,
  onStale,
}: PrReviewBarProps) {
  const [summary, setSummary] = useState("");
  const [confirming, setConfirming] = useState<ReviewEventName | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const buttons = eventButtons({
    allowedEvents: pr.allowed_events,
    isAuthor: pr.viewer?.is_author ?? false,
    drafts: drafts.length,
    stale: stale.length,
    summary,
    busy,
  });
  const firstStale = stale[0];

  async function submit(event: ReviewEventName) {
    setBusy(true);
    setError(null);
    try {
      const result = await api.submitReview(pr.review_id, event, summary.trim());
      setSummary("");
      setConfirming(null);
      onSubmitted(result, event);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        const ids = staleIdsFrom(e.body);
        onStale(ids);
        setError(
          `${e.message} Re-anchor or delete ${plural(ids.length, "stale comment")}, then submit again.`,
        );
      } else {
        setError(errorMessage(e));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <footer className="review-bar pr-review-bar">
      <textarea
        className="overall-input"
        rows={1}
        aria-label="Review summary"
        placeholder="Review summary (markdown)"
        value={summary}
        onChange={(event) => setSummary(event.target.value)}
      />
      <div className="review-bar-status" aria-live="polite">
        <span className={drafts.length > 0 ? "pill pill-draft" : "pill"}>
          {plural(drafts.length, "draft")}
        </span>
        {firstStale && (
          <button
            type="button"
            className="pill pill-stale"
            title={`Jump to ${threadLocation(firstStale)}`}
            onClick={() => onJumpToThread(firstStale)}
          >
            {staleBlockLabel(stale.length)} · jump
          </button>
        )}
      </div>
      <div className="review-buttons" role="group" aria-label="Submit review">
        {buttons.map((button) => (
          <span key={button.event} title={button.reason ?? undefined} className="button-wrap">
            <button
              type="button"
              className={button.event === "APPROVE" ? "button primary" : "button"}
              disabled={button.disabled}
              onClick={() => {
                setError(null);
                setConfirming(button.event);
              }}
            >
              {button.label}
            </button>
          </span>
        ))}
      </div>
      {confirming && (
        <ConfirmDialog
          content={confirmContent(confirming, summary, drafts)}
          busy={busy}
          error={error}
          onConfirm={() => void submit(confirming)}
          onCancel={() => setConfirming(null)}
        />
      )}
    </footer>
  );
}
