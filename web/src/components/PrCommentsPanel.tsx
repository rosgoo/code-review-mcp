import { useState } from "react";
import { questionLocation, questionState, stagedMessages } from "../lib/agent";
import { previewBody, threadLocation } from "../lib/review";
import type { ReviewThread } from "../lib/types";
import { useAgentLive, usePrCommentActions, usePrCommentState } from "./PrCommentContext";
import { QuestionStateChip } from "./QuestionThread";
import { StatusBadge, errorMessage } from "./Thread";

export type PanelTab = "drafts" | "questions";

interface PrCommentsPanelProps {
  tab: PanelTab;
  drafts: readonly ReviewThread[];
  questions: readonly ReviewThread[];
  onTab(tab: PanelTab): void;
  onJump(thread: ReviewThread): void;
  onClose(): void;
}

function QuestionItem({
  thread,
  onJump,
}: {
  thread: ReviewThread;
  onJump(thread: ReviewThread): void;
}) {
  const live = useAgentLive();
  const state = questionState(thread, live);
  const last = thread.messages.at(-1);
  const preview = live.streams[thread.id] ?? last?.body ?? "";
  const staged = stagedMessages(thread).length;
  return (
    <button
      type="button"
      className={`panel-item panel-item-question question-${state.kind}`}
      onClick={() => onJump(thread)}
    >
      <span className="panel-item-header">
        <span className="badge badge-question">Question</span>
        <span className="thread-location">{questionLocation(thread)}</span>
        <span className="spacer" />
        <QuestionStateChip state={state} />
      </span>
      {staged > 1 && <span className="muted panel-staged-note">{staged} staged messages</span>}
      <span className="panel-preview">
        {last?.author === "agent" || live.streams[thread.id] ? "Agent: " : "You: "}
        {previewBody(preview)}
      </span>
    </button>
  );
}

/** Drafts and stale comments, and questions to the agent, across every file. */
export function PrCommentsPanel({
  tab,
  drafts,
  questions,
  onTab,
  onJump,
  onClose,
}: PrCommentsPanelProps) {
  return (
    <aside className="comments-panel pr-comments-panel" aria-label="Comments and questions">
      <header className="panel-header">
        <div className="panel-tabs" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "drafts"}
            className={tab === "drafts" ? "active" : ""}
            onClick={() => onTab("drafts")}
          >
            Drafts <span className="count">{drafts.length}</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "questions"}
            className={tab === "questions" ? "active" : ""}
            onClick={() => onTab("questions")}
          >
            Questions <span className="count">{questions.length}</span>
          </button>
        </div>
        <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>
          ×
        </button>
      </header>
      <div className="panel-body" role="tabpanel">
        {tab === "drafts" && drafts.length === 0 && (
          <p className="muted panel-empty">
            No drafts. Click + next to a highlighted line, or select lines, to write one.
          </p>
        )}
        {tab === "drafts" &&
          drafts.map((thread) => (
            <button
              key={thread.id}
              type="button"
              className={`panel-item panel-item-${thread.status}`}
              onClick={() => onJump(thread)}
            >
              <span className="panel-item-header">
                <StatusBadge status={thread.status} />
                <span className="thread-location">{threadLocation(thread)}</span>
                {thread.created_by === "agent" && (
                  <span className="tag tag-agent">Drafted by agent</span>
                )}
              </span>
              <span className="panel-preview">{previewBody(thread.messages[0]?.body ?? "")}</span>
            </button>
          ))}
        {tab === "questions" && <QuestionsTab questions={questions} onJump={onJump} />}
      </div>
    </aside>
  );
}

function QuestionsTab({
  questions,
  onJump,
}: {
  questions: readonly ReviewThread[];
  onJump(thread: ReviewThread): void;
}) {
  const actions = usePrCommentActions();
  const { agentOn } = usePrCommentState();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const staged = questions.filter((t) => stagedMessages(t).length > 0);
  const rest = questions.filter((t) => stagedMessages(t).length === 0);
  if (questions.length === 0) {
    return (
      <p className="muted panel-empty">
        No questions. Ask the agent from a line, a file header, or the agent panel.
      </p>
    );
  }

  async function send() {
    setBusy(true);
    setError(null);
    try {
      await actions.send();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {staged.length > 0 && (
        <div className="panel-section">
          <div className="panel-section-header">
            <span>Staged</span>
            <span className="spacer" />
            <button
              type="button"
              className="button button-small agent-send"
              disabled={busy || !agentOn}
              onClick={() => void send()}
            >
              Send to agent
            </button>
          </div>
          {error && <p className="form-error">{error}</p>}
          {staged.map((thread) => (
            <QuestionItem key={thread.id} thread={thread} onJump={onJump} />
          ))}
        </div>
      )}
      {rest.length > 0 && (
        <div className="panel-section">
          <div className="panel-section-header">
            <span>Asked</span>
          </div>
          {rest.map((thread) => (
            <QuestionItem key={thread.id} thread={thread} onJump={onJump} />
          ))}
        </div>
      )}
    </>
  );
}
