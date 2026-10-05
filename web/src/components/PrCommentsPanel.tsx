import { previewBody, threadLocation } from "../lib/review";
import type { ReviewThread } from "../lib/types";
import { StatusBadge } from "./Thread";

interface PrCommentsPanelProps {
  threads: readonly ReviewThread[];
  onJump(thread: ReviewThread): void;
  onClose(): void;
}

/** Drafts and stale comments across every file, in file order. */
export function PrCommentsPanel({ threads, onJump, onClose }: PrCommentsPanelProps) {
  return (
    <aside className="comments-panel pr-comments-panel" aria-label="Draft comments">
      <header className="panel-header">
        <span>Drafts and stale comments</span>
        <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>
          ×
        </button>
      </header>
      <div className="panel-body">
        {threads.length === 0 && (
          <p className="muted panel-empty">
            No drafts. Click + next to a highlighted line, or select lines, to write one.
          </p>
        )}
        {threads.map((thread) => (
          <button
            key={thread.id}
            type="button"
            className={`panel-item panel-item-${thread.status}`}
            onClick={() => onJump(thread)}
          >
            <span className="panel-item-header">
              <StatusBadge status={thread.status} />
              <span className="thread-location">{threadLocation(thread)}</span>
            </span>
            <span className="panel-preview">{previewBody(thread.messages[0]?.body ?? "")}</span>
          </button>
        ))}
      </div>
    </aside>
  );
}
