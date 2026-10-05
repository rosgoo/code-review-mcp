import { useState } from "react";
import type { AnnotationData } from "../lib/annotations";
import { locationLabel, threadLocation } from "../lib/review";
import type { ReviewAnchor, ReviewThread } from "../lib/types";
import { Markdown } from "./Markdown";
import { MarkdownEditor } from "./MarkdownEditor";
import { usePrCommentActions, usePrCommentState } from "./PrCommentContext";
import { StatusBadge, errorMessage } from "./Thread";

export const reviewThreadDomId = (threadId: string) => `review-thread-${threadId}`;

export function ReviewThreadCard({ thread }: { thread: ReviewThread }) {
  const actions = usePrCommentActions();
  const { reanchoring } = usePrCommentState();
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [first, ...replies] = thread.messages;
  const moving = reanchoring === thread.id;

  async function remove() {
    setError(null);
    try {
      await actions.deleteComment(thread.id);
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <article
      id={reviewThreadDomId(thread.id)}
      className={`thread review-thread thread-${thread.status}${moving ? " moving" : ""}`}
    >
      <header className="thread-header">
        <StatusBadge status={thread.status} />
        <span className="thread-location">{threadLocation(thread)}</span>
        {thread.created_by === "agent" && <span className="tag">by agent</span>}
        <span className="spacer" />
        {thread.status === "draft" && !editing && (
          <>
            <button type="button" className="link-button" onClick={() => setEditing(true)}>
              Edit
            </button>
            <button type="button" className="link-button danger" onClick={() => void remove()}>
              Delete
            </button>
          </>
        )}
        {thread.status === "stale" && (
          <>
            <button
              type="button"
              className="link-button"
              onClick={() => actions.startReanchor(moving ? null : thread.id)}
            >
              {moving ? "Cancel re-anchor" : "Re-anchor"}
            </button>
            <button type="button" className="link-button danger" onClick={() => void remove()}>
              Delete
            </button>
          </>
        )}
        {thread.status === "posted" && thread.github_url && (
          <a href={thread.github_url} target="_blank" rel="noreferrer noopener">
            View on GitHub ↗
          </a>
        )}
      </header>
      {thread.status === "stale" && (
        <p className="stale-note">
          The code changed after you wrote this.
          {moving && " Click + on a highlighted line in this file to move the comment there."}
        </p>
      )}
      {error && <p className="form-error">{error}</p>}
      {editing && first ? (
        <MarkdownEditor
          initial={first.body}
          placeholder="Edit the comment"
          submitLabel="Save"
          onSubmit={async (body) => {
            await actions.editComment(thread.id, body);
            setEditing(false);
          }}
          onCancel={() => setEditing(false)}
        />
      ) : (
        first && <Markdown text={first.body} className="review-comment-body" />
      )}
      {replies.map((message) => (
        <div
          key={message.id}
          className={`message message-${message.author === "agent" ? "claude" : "user"}`}
        >
          <span className="message-author">{message.author === "agent" ? "Agent" : "You"}</span>
          <Markdown text={message.body} />
        </div>
      ))}
    </article>
  );
}

export function ReviewComposerCard({ anchor }: { anchor: ReviewAnchor }) {
  const actions = usePrCommentActions();
  return (
    <div className="composer review-composer">
      <div className="composer-label">
        Draft review comment · {locationLabel(anchor)}
        {anchor.side === "deletions" && anchor.line > 0 && " · old side"}
      </div>
      <MarkdownEditor
        placeholder="Leave a review comment for the author"
        submitLabel="Add draft"
        onSubmit={(body) => actions.addComment(anchor, body)}
        onCancel={() => actions.openComposer(null)}
      />
    </div>
  );
}

export function ReviewAnnotation({ data }: { data: AnnotationData | undefined }) {
  const { threadsById, composer } = usePrCommentState();
  if (data === undefined) return null;
  const threads = data.threadIds.flatMap((id) => threadsById.get(id) ?? []);
  return (
    <div className="annotation">
      {threads.map((thread) => (
        <ReviewThreadCard key={thread.id} thread={thread} />
      ))}
      {data.composer && composer && <ReviewComposerCard anchor={composer} />}
    </div>
  );
}

/** File-level comments, stale drafts, and outdated posted comments, above the diff. */
export function FileThreadsBlock({
  threads,
  composer,
}: {
  threads: readonly ReviewThread[];
  composer: ReviewAnchor | null;
}) {
  const fileComposer = composer !== null && composer.line === 0 ? composer : null;
  if (threads.length === 0 && fileComposer === null) return null;
  return (
    <div className="file-threads">
      {threads.map((thread) => (
        <ReviewThreadCard key={thread.id} thread={thread} />
      ))}
      {fileComposer && <ReviewComposerCard anchor={fileComposer} />}
    </div>
  );
}
