import { useState, type KeyboardEvent } from "react";
import {
  questionLocation,
  questionState,
  questionStateLabel,
  type QuestionState,
} from "../lib/agent";
import { reviewThreadDomId } from "../lib/review";
import type { ReviewThread } from "../lib/types";
import { Markdown } from "./Markdown";
import { MarkdownEditor } from "./MarkdownEditor";
import { useAgentLive, usePrCommentActions, usePrCommentState } from "./PrCommentContext";
import { errorMessage } from "./Thread";

export function TypingDots({ label = "The agent is writing" }: { label?: string }) {
  return (
    <span className="typing-dots" role="status" aria-label={label}>
      <span />
      <span />
      <span />
    </span>
  );
}

export function QuestionStateChip({ state }: { state: QuestionState }) {
  return <span className={`chip chip-${state.kind}`}>{questionStateLabel(state)}</span>;
}

/** A question to the agent: the messages, the answer streaming in, and the follow-up box. */
export function QuestionThreadCard({ thread }: { thread: ReviewThread }) {
  const actions = usePrCommentActions();
  const { agentOn, citations } = usePrCommentState();
  const live = useAgentLive();
  const state = questionState(thread, live);
  const stream = live.streams[thread.id];
  const [replying, setReplying] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function act(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const streaming = state.kind === "running" || stream !== undefined;
  return (
    <article
      id={reviewThreadDomId(thread.id)}
      className={`thread question-thread question-${state.kind}`}
    >
      <header className="thread-header">
        <span className="badge badge-question">Question</span>
        <span className="thread-location">{questionLocation(thread)}</span>
        <span className="muted question-private" title="Only you see questions to the agent">
          private
        </span>
        <span className="spacer" />
        <QuestionStateChip state={state} />
        {state.kind === "running" && (
          <button
            type="button"
            className="button button-small"
            disabled={busy}
            onClick={() => void act(() => actions.stop(thread.id))}
          >
            Stop
          </button>
        )}
      </header>
      {thread.messages.map((message) => (
        <div
          key={message.id}
          className={`message ${message.author === "agent" ? "message-agent" : "message-user"}`}
        >
          <span className="message-author">{message.author === "agent" ? "Agent" : "You"}</span>
          <Markdown
            text={message.body}
            citations={message.author === "agent" ? citations : undefined}
          />
        </div>
      ))}
      {streaming && (
        <div className="message message-agent streaming" aria-live="polite">
          <span className="message-author">Agent</span>
          {stream && <Markdown text={stream} citations={citations} />}
          <TypingDots />
        </div>
      )}
      {state.kind === "error" && (
        <div className="question-problem" role="alert">
          <span>The agent could not answer: {state.error}</span>
          {agentOn && (
            <button
              type="button"
              className="button button-small"
              disabled={busy}
              onClick={() => void act(() => actions.retry(thread))}
            >
              Retry
            </button>
          )}
        </div>
      )}
      {state.kind === "waiting" && (
        <div className="question-problem muted">
          <span>{agentOn ? "No answer yet." : "No answer yet. The review agent is off."}</span>
          {agentOn && (
            <button
              type="button"
              className="button button-small"
              disabled={busy}
              onClick={() => void act(() => actions.retry(thread))}
            >
              Retry
            </button>
          )}
        </div>
      )}
      {error && <p className="form-error">{error}</p>}
      {replying ? (
        <MarkdownEditor
          placeholder="Ask a follow-up"
          submitLabel="Ask"
          onSubmit={async (body) => {
            await actions.reply(thread.id, body);
            setReplying(false);
          }}
          onCancel={() => setReplying(false)}
        />
      ) : (
        agentOn && (
          <button type="button" className="link-button" onClick={() => setReplying(true)}>
            Follow up
          </button>
        )
      )}
    </article>
  );
}

/** A box that asks the agent a question about the whole PR. */
export function AskBox({
  disabled,
  onAsk,
}: {
  disabled: boolean;
  onAsk(body: string): Promise<void>;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    const body = text.trim();
    if (!body || busy || disabled) return;
    setBusy(true);
    setError(null);
    try {
      await onAsk(body);
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
    }
  }

  return (
    <div className="ask-box">
      <textarea
        rows={2}
        value={text}
        placeholder="Ask about this PR. Only you see the question and the answer."
        aria-label="Ask about this PR"
        disabled={busy || disabled}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={onKeyDown}
      />
      <div className="form-actions">
        {error && <span className="form-error">{error}</span>}
        <span className="form-hint">⌘/Ctrl + Enter</span>
        <button
          type="button"
          className="button ask-button"
          disabled={busy || disabled || !text.trim()}
          onClick={() => void submit()}
        >
          {busy ? "Asking…" : "Ask"}
        </button>
      </div>
    </div>
  );
}
