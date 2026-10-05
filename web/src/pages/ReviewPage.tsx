import { useCallback, useEffect, useMemo, useState } from "react";
import { CommentsPanel } from "../components/CommentsPanel";
import { DiffStyleToggle } from "../components/DiffStyleToggle";
import { DiffEntryView, fileDomId, PlainFileView } from "../components/FileViews";
import { FileTree } from "../components/FileTree";
import { Link } from "../components/Link";
import { ReviewBar } from "../components/ReviewBar";
import { ReviewContext, type ReviewActions } from "../components/ReviewContext";
import { errorMessage, threadDomId, ThreadCard } from "../components/Thread";
import { sameAnchor, toNewComment, type CommentAnchor } from "../lib/anchors";
import { api } from "../lib/api";
import { applyCommentEvent, OVERALL_PATH } from "../lib/comments";
import { fileEntries } from "../lib/entries";
import type { ChangeCounts } from "../lib/patch";
import { jumpTo } from "../lib/dom";
import { useDiffStyle, useReviewEvents } from "../lib/hooks";
import type { Comment, ReviewView } from "../lib/types";

export function ReviewPage({
  reviewId,
  initialView,
}: {
  reviewId: string;
  initialView: ReviewView;
}) {
  const [view, setView] = useState<ReviewView | null>(initialView);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [comments, setComments] = useState<Comment[]>([]);
  const [composer, setComposer] = useState<CommentAnchor | null>(null);
  const [diffStyle, setDiffStyle] = useDiffStyle();
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [panelOpen, setPanelOpen] = useState(false);

  const loadView = useCallback(async () => {
    try {
      setView(await api.view(reviewId));
      setLoadError(null);
    } catch (e) {
      setLoadError(errorMessage(e));
    }
  }, [reviewId]);

  const loadComments = useCallback(async () => {
    try {
      setComments(await api.comments(reviewId));
    } catch {
      // The view load reports a missing review; a failed refresh keeps the last list.
    }
  }, [reviewId]);

  useEffect(() => {
    void loadComments();
  }, [loadComments]);

  useReviewEvents(reviewId, (event) => {
    if (event.type === "view_updated") {
      void loadView();
      void loadComments();
    } else if (event.type === "comments_submitted") {
      void loadComments();
    } else {
      setComments((current) => applyCommentEvent(current, event));
    }
  });

  useEffect(() => {
    document.title = view ? `${view.title} · Code Review` : "Code Review";
  }, [view]);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setPanelOpen(false);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const entries = useMemo(() => (view ? fileEntries(view) : []), [view]);
  const paths = useMemo(() => entries.map((entry) => entry.path), [entries]);
  const counts = useMemo(
    () =>
      new Map<string, ChangeCounts>(
        entries.flatMap((entry) => (entry.kind === "plain" ? [] : [[entry.path, entry.counts]])),
      ),
    [entries],
  );
  const overallThreads = useMemo(
    () => comments.filter((c) => c.file_path === OVERALL_PATH),
    [comments],
  );
  const commentsById = useMemo(() => new Map(comments.map((c) => [c.id, c])), [comments]);

  const openComposer = useCallback((anchor: CommentAnchor | null) => {
    setComposer((current) => (sameAnchor(current, anchor) ? current : anchor));
  }, []);

  const actions = useMemo<Omit<ReviewActions, "commentsById" | "composer">>(
    () => ({
      openComposer,
      async saveComment(anchor, body) {
        await api.addComment(reviewId, toNewComment(anchor, body));
        setComposer((current) => (sameAnchor(current, anchor) ? null : current));
        await loadComments();
      },
      async reply(threadId, message) {
        await api.reply(threadId, message);
        await loadComments();
      },
      async deleteThread(threadId) {
        await api.deleteThread(threadId);
        setComments((current) => current.filter((c) => c.id !== threadId));
      },
    }),
    [reviewId, openComposer, loadComments],
  );
  const context = useMemo<ReviewActions>(
    () => ({ ...actions, commentsById, composer }),
    [actions, commentsById, composer],
  );

  const showsDiff = entries.some((entry) => entry.kind !== "plain");
  const openThreads = comments.filter((c) => c.status !== "resolved").length;

  let body;
  if (loadError !== null) {
    body = (
      <div className="empty-state">
        <p>Could not load this review: {loadError}</p>
        <Link to="/">Back to all reviews</Link>
      </div>
    );
  } else if (view === null) {
    body = <div className="empty-state muted">Loading…</div>;
  } else if (entries.length === 0) {
    body = <div className="empty-state muted">Waiting for the agent to send code…</div>;
  } else {
    body = entries.map((entry) =>
      entry.kind === "plain" ? (
        <PlainFileView
          key={entry.path}
          file={entry.file}
          comments={comments}
          composer={composer}
          onAnchor={openComposer}
        />
      ) : (
        <DiffEntryView
          key={entry.path}
          entry={entry}
          diffStyle={diffStyle}
          comments={comments}
          composer={composer}
          onAnchor={openComposer}
        />
      ),
    );
  }

  return (
    <ReviewContext.Provider value={context}>
      <div className="review-page">
        <header className="topbar">
          <button
            type="button"
            className="icon-button"
            aria-label="Toggle file tree"
            aria-pressed={sidebarOpen}
            onClick={() => setSidebarOpen(!sidebarOpen)}
          >
            ☰
          </button>
          <Link to="/" className="topbar-home">
            Reviews
          </Link>
          <span className="topbar-sep">/</span>
          <h1 className="topbar-title" title={view?.title}>
            {view?.title ?? "…"}
          </h1>
          <span className="spacer" />
          {showsDiff && <DiffStyleToggle value={diffStyle} onChange={setDiffStyle} />}
          <button
            type="button"
            className="button"
            aria-expanded={panelOpen}
            onClick={() => setPanelOpen(!panelOpen)}
          >
            Comments <span className="count">{openThreads}</span>
          </button>
        </header>
        <div className={sidebarOpen ? "review-body" : "review-body no-sidebar"}>
          {sidebarOpen && (
            <FileTree paths={paths} counts={counts} onSelect={(path) => jumpTo(fileDomId(path))} />
          )}
          <main className="review-main">
            {overallThreads.length > 0 && (
              <section className="overall">
                <h2>Overall feedback</h2>
                {overallThreads.map((comment) => (
                  <ThreadCard key={comment.id} comment={comment} />
                ))}
              </section>
            )}
            {body}
          </main>
        </div>
        {panelOpen && (
          <CommentsPanel
            comments={comments}
            onJump={(threadId) => jumpTo(threadDomId(threadId))}
            onClose={() => setPanelOpen(false)}
          />
        )}
        <ReviewBar reviewId={reviewId} comments={comments} onSubmitted={loadComments} />
      </div>
    </ReviewContext.Provider>
  );
}
