import { useState, type KeyboardEvent } from "react";
import type { CommentAnchor } from "../lib/anchors";
import type { AnnotationData } from "../lib/annotations";
import { lineLabel } from "../lib/comments";
import type { Comment, ThreadStatus } from "../lib/types";
import { Markdown } from "./Markdown";
import { useReview } from "./ReviewContext";

export const threadDomId = (threadId: string) => `thread-${threadId}`;

export const errorMessage = (error: unknown) =>
  error instanceof Error ? error.message : String(error);

interface TextFormProps {
  placeholder: string;
  submitLabel: string;
  onSubmit(text: string): Promise<void>;
  onCancel(): void;
}

export function TextForm({ placeholder, submitLabel, onSubmit, onCancel }: TextFormProps) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    const body = text.trim();
    if (!body || busy) return;
    setBusy(true);
    setError(null);
    try {
      await onSubmit(body);
      setText("");
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      void submit();
    } else if (event.key === "Escape") {
      event.preventDefault();
      onCancel();
    }
  }

  return (
    <div className="text-form">
      <textarea
        autoFocus
        rows={3}
        value={text}
        placeholder={placeholder}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={onKeyDown}
        disabled={busy}
      />
      {error && <p className="form-error">{error}</p>}
      <div className="form-actions">
        <span className="form-hint">⌘/Ctrl + Enter</span>
        <button type="button" className="button" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button
          type="button"
          className="button primary"
          onClick={() => void submit()}
          disabled={busy || !text.trim()}
        >
          {submitLabel}
        </button>
      </div>
    </div>
  );
}

export function StatusBadge({ status }: { status: ThreadStatus }) {
  return <span className={`badge badge-${status}`}>{status}</span>;
}

export function ThreadCard({ comment }: { comment: Comment }) {
  const { reply, deleteThread } = useReview();
  const [replying, setReplying] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  async function remove() {
    setDeleteError(null);
    try {
      await deleteThread(comment.id);
    } catch (e) {
      setDeleteError(errorMessage(e));
    }
  }

  return (
    <article id={threadDomId(comment.id)} className={`thread thread-${comment.status}`}>
      <header className="thread-header">
        <StatusBadge status={comment.status} />
        <span className="thread-location">{lineLabel(comment)}</span>
        <span className="spacer" />
        {comment.status === "draft" && (
          <button type="button" className="link-button danger" onClick={() => void remove()}>
            Delete
          </button>
        )}
      </header>
      {deleteError && <p className="form-error">{deleteError}</p>}
      <div className="message message-user">
        <span className="message-author">You</span>
        <p className="message-text">{comment.user_message}</p>
      </div>
      {comment.replies.map((r) => (
        <div key={r.id} className={`message message-${r.author}`}>
          <span className="message-author">{r.author === "claude" ? "Agent" : "You"}</span>
          {r.author === "claude" ? (
            <Markdown text={r.message} />
          ) : (
            <p className="message-text">{r.message}</p>
          )}
        </div>
      ))}
      {replying ? (
        <TextForm
          placeholder="Reply…"
          submitLabel="Reply"
          onSubmit={async (text) => {
            await reply(comment.id, text);
            setReplying(false);
          }}
          onCancel={() => setReplying(false)}
        />
      ) : (
        <button type="button" className="link-button" onClick={() => setReplying(true)}>
          {comment.status === "resolved" ? "Reply (reopens)" : "Reply"}
        </button>
      )}
    </article>
  );
}

function anchorLabel(anchor: CommentAnchor): string {
  if (anchor.startLine === undefined) return `Line ${anchor.line}`;
  const sides = anchor.startSide !== anchor.side ? ` (${anchor.startSide} → ${anchor.side})` : "";
  return `Lines ${anchor.startLine}–${anchor.line}${sides}`;
}

export function Composer({ anchor }: { anchor: CommentAnchor }) {
  const { saveComment, openComposer } = useReview();
  return (
    <div className="composer">
      <div className="composer-label">
        {anchorLabel(anchor)}
        {anchor.side === "deletions" && " · old side"}
      </div>
      <TextForm
        placeholder="Leave a comment for the agent"
        submitLabel="Add comment"
        onSubmit={(body) => saveComment(anchor, body)}
        onCancel={() => openComposer(null)}
      />
    </div>
  );
}

export function AnnotationSlot({ data }: { data: AnnotationData | undefined }) {
  const { commentsById, composer } = useReview();
  if (data === undefined) return null;
  const threads = data.threadIds.flatMap((id) => commentsById.get(id) ?? []);
  return (
    <div className="annotation">
      {threads.map((comment) => (
        <ThreadCard key={comment.id} comment={comment} />
      ))}
      {data.composer && composer && <Composer anchor={composer} />}
    </div>
  );
}
