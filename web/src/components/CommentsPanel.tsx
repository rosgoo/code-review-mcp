import { useMemo } from "react";
import { lineLabel } from "../lib/comments";
import type { Comment } from "../lib/types";
import { StatusBadge } from "./Thread";

const PREVIEW_LENGTH = 80;

interface CommentsPanelProps {
  comments: readonly Comment[];
  onJump(threadId: string): void;
  onClose(): void;
}

export function CommentsPanel({ comments, onJump, onClose }: CommentsPanelProps) {
  const groups = useMemo(() => {
    const sorted = [...comments].sort(
      (a, b) => a.file_path.localeCompare(b.file_path) || a.line_number - b.line_number,
    );
    const byPath = new Map<string, Comment[]>();
    for (const comment of sorted) {
      byPath.set(comment.file_path, [...(byPath.get(comment.file_path) ?? []), comment]);
    }
    return [...byPath.entries()];
  }, [comments]);

  return (
    <aside className="comments-panel" aria-label="Comments">
      <header className="panel-header">
        <span>Comments</span>
        <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>
          ×
        </button>
      </header>
      <div className="panel-body">
        {groups.length === 0 && (
          <p className="muted panel-empty">Click + next to a line, or select lines, to comment.</p>
        )}
        {groups.map(([path, threads]) => (
          <section key={path}>
            <h3 className="panel-file">{path}</h3>
            {threads.map((comment) => (
              <button
                key={comment.id}
                type="button"
                className={`panel-item panel-item-${comment.status}`}
                onClick={() => onJump(comment.id)}
              >
                <span className="panel-item-header">
                  <StatusBadge status={comment.status} />
                  <span className="thread-location">{lineLabel(comment)}</span>
                  {comment.replies.length > 0 && (
                    <span className="muted">
                      {comment.replies.length} repl{comment.replies.length === 1 ? "y" : "ies"}
                    </span>
                  )}
                </span>
                <span className="panel-preview">
                  {comment.user_message.length > PREVIEW_LENGTH
                    ? `${comment.user_message.slice(0, PREVIEW_LENGTH)}…`
                    : comment.user_message}
                </span>
              </button>
            ))}
          </section>
        ))}
      </div>
    </aside>
  );
}
