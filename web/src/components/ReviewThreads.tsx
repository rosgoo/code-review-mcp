import { useState } from "react";
import { composerAnchor, type Composer } from "../lib/agent";
import type { AnnotationData } from "../lib/annotations";
import { locationLabel, reviewThreadDomId, threadLocation } from "../lib/review";
import type { ComposerMode, ReviewThread } from "../lib/types";
import { Markdown } from "./Markdown";
import { MarkdownEditor } from "./MarkdownEditor";
import { usePrCommentActions, usePrCommentState } from "./PrCommentContext";
import { QuestionThreadCard } from "./QuestionThread";
import { StatusBadge, errorMessage } from "./Thread";

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
        {thread.created_by === "agent" && (
          <span className="tag tag-agent" title="The review agent drafted this comment">
            Drafted by agent
          </span>
        )}
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

const OUTSIDE_DIFF_TITLE = "Review comments go on lines inside the diff";

function ModeToggle({ composer }: { composer: Composer }) {
  const actions = usePrCommentActions();
  const { agentOn } = usePrCommentState();
  const option = (mode: ComposerMode, label: string, disabledReason: string | null) => (
    <button
      type="button"
      role="radio"
      aria-checked={composer.mode === mode}
      className={composer.mode === mode ? `active mode-${mode}` : `mode-${mode}`}
      disabled={disabledReason !== null}
      title={disabledReason ?? undefined}
      onClick={() => actions.setComposerMode(mode)}
    >
      {label}
    </button>
  );
  return (
    <div className="segmented mode-toggle" role="radiogroup" aria-label="What to write">
      {option("comment", "Review comment", composer.comment === null ? OUTSIDE_DIFF_TITLE : null)}
      {option("question", "Ask agent", agentOn ? null : "The review agent is off")}
    </div>
  );
}

export function ReviewComposerCard({ composer }: { composer: Composer }) {
  const actions = usePrCommentActions();
  const anchor = composerAnchor(composer);
  const asking = composer.mode === "question";
  const where = anchor.line === 0 ? `${anchor.path} (file)` : locationLabel(anchor);
  return (
    <div className={asking ? "composer review-composer composer-question" : "composer review-composer"}>
      <div className="composer-label">
        <ModeToggle composer={composer} />
        <span>
          {asking ? "Ask agent" : "Draft review comment"} · {where}
          {anchor.side === "deletions" && anchor.line > 0 && " · old side"}
        </span>
      </div>
      {asking && (
        <p className="composer-note">
          Private: only you see this. The agent answers here and posts nothing to GitHub.
        </p>
      )}
      <MarkdownEditor
        placeholder={asking ? "Ask the agent about this code" : "Leave a review comment for the author"}
        submitLabel={asking ? "Ask" : "Add draft"}
        onSubmit={(body) =>
          asking ? actions.askQuestion(anchor, body) : actions.addComment(anchor, body)
        }
        onCancel={() => actions.openComposer(null)}
      />
    </div>
  );
}

function AnyThreadCard({ thread }: { thread: ReviewThread }) {
  return thread.kind === "question" ? (
    <QuestionThreadCard thread={thread} />
  ) : (
    <ReviewThreadCard thread={thread} />
  );
}

export function ReviewAnnotation({ data }: { data: AnnotationData | undefined }) {
  const { threadsById, composer } = usePrCommentState();
  if (data === undefined) return null;
  const threads = data.threadIds.flatMap((id) => threadsById.get(id) ?? []);
  return (
    <div className="annotation">
      {threads.map((thread) => (
        <AnyThreadCard key={thread.id} thread={thread} />
      ))}
      {data.composer && composer && <ReviewComposerCard composer={composer} />}
    </div>
  );
}

/** File-level threads, stale drafts, outdated comments, and threads on hidden lines, above the diff. */
export function FileThreadsBlock({
  threads,
  composer,
}: {
  threads: readonly ReviewThread[];
  composer: Composer | null;
}) {
  const fileComposer = composer !== null && composerAnchor(composer).line === 0 ? composer : null;
  if (threads.length === 0 && fileComposer === null) return null;
  return (
    <div className="file-threads">
      {threads.map((thread) => (
        <AnyThreadCard key={thread.id} thread={thread} />
      ))}
      {fileComposer && <ReviewComposerCard composer={fileComposer} />}
    </div>
  );
}
