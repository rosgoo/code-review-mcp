import { useState, type KeyboardEvent } from "react";
import {
  isStaged,
  questionLocation,
  questionState,
  questionStateLabel,
  type QuestionState,
} from "../lib/agent";
import { reviewThreadDomId } from "../lib/review";
import type { ReviewThread, ThreadMessage } from "../lib/types";
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

function MessageView({ thread, message }: { thread: ReviewThread; message: ThreadMessage }) {
  const actions = usePrCommentActions();
  const { citations } = usePrCommentState();
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const staged = isStaged(message);
  const agent = message.author === "agent";

  async function remove() {
    setError(null);
    try {
      await actions.deleteStaged(thread, message);
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <div
      className={`message ${agent ? "message-agent" : "message-user"}${staged ? " message-staged" : ""}`}
    >
      <span className="message-author">
        {agent ? "Agent" : "You"}
        {staged && <span className="badge badge-staged">Staged</span>}
        {staged && !editing && (
          <span className="message-actions">
            <button type="button" className="link-button" onClick={() => setEditing(true)}>
              Edit
            </button>
            <button type="button" className="link-button danger" onClick={() => void remove()}>
              Delete
            </button>
          </span>
        )}
      </span>
      {editing ? (
        <MarkdownEditor
          initial={message.body}
          placeholder="Edit the question"
          submitLabel="Save"
          onSubmit={async (body) => {
            await actions.editStaged(thread, message, body);
            setEditing(false);
          }}
          onCancel={() => setEditing(false)}
        />
      ) : (
        <Markdown text={message.body} citations={agent ? citations : undefined} />
      )}
      {error && <p className="form-error">{error}</p>}
    </div>
  );
}

/** A question to the agent: its messages, staged ones editable, and the answer as it streams. */
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

  const lastQuestion = thread.messages.findLast((m) => m.author === "user")?.body;
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
        {state.kind === "staged" && state.error === null && agentOn && (
          <button
            type="button"
            className="button button-small ask-button"
            disabled={busy}
            title="Send this question to the agent now, without the other staged ones"
            onClick={() => void act(() => actions.send([thread.id]))}
          >
            Send now
          </button>
        )}
      </header>
      {thread.messages.map((message) => (
        <MessageView key={message.id} thread={thread} message={message} />
      ))}
      {state.kind === "streaming" && (
        <div className="message message-agent streaming" aria-live="polite">
          <span className="message-author">Agent</span>
          {stream && <Markdown text={stream} citations={citations} />}
          <TypingDots />
        </div>
      )}
      {state.kind === "waiting" && (
        <div className="agent-waiting">
          <TypingDots label="Waiting for the agent" /> Waiting for the agent
        </div>
      )}
      {state.kind === "staged" && state.error !== null && (
        <div className="question-problem" role="alert">
          <span>{state.error.replace(/\.?$/, ".")} The question is staged again.</span>
          {agentOn && (
            <button
              type="button"
              className="button button-small ask-button"
              disabled={busy}
              title="Send this question to the agent again"
              onClick={() => void act(() => actions.send([thread.id]))}
            >
              Re-send
            </button>
          )}
        </div>
      )}
      {state.kind === "error" && (
        <p className="question-problem" role="alert">
          The agent could not answer: {state.error}
        </p>
      )}
      {(state.kind === "error" || state.kind === "unanswered") && agentOn && lastQuestion && (
        <div className="question-problem muted">
          <span>{state.kind === "unanswered" ? "No answer yet." : ""}</span>
          <button
            type="button"
            className="button button-small"
            disabled={busy}
            onClick={() => void act(() => actions.reply(thread.id, lastQuestion, true))}
          >
            Ask again
          </button>
        </div>
      )}
      {error && <p className="form-error">{error}</p>}
      {replying ? (
        <MarkdownEditor
          placeholder="Ask a follow-up"
          submitLabel="Add to batch"
          onSubmit={async (body) => {
            await actions.reply(thread.id, body, false);
            setReplying(false);
          }}
          secondary={{
            label: "Ask now",
            onSubmit: async (body) => {
              await actions.reply(thread.id, body, true);
              setReplying(false);
            },
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

/** A box that stages a question about the whole PR, or asks it at once. */
export function AskBox({
  disabled,
  onAsk,
}: {
  disabled: boolean;
  onAsk(body: string, now: boolean): Promise<void>;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(now: boolean) {
    const body = text.trim();
    if (!body || busy || disabled) return;
    setBusy(true);
    setError(null);
    try {
      await onAsk(body, now);
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
      void submit(false);
    }
  }

  const empty = !text.trim();
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
        <span className="form-hint">⌘/Ctrl + Enter adds to the batch</span>
        <button
          type="button"
          className="button"
          disabled={busy || disabled || empty}
          onClick={() => void submit(true)}
        >
          Ask now
        </button>
        <button
          type="button"
          className="button ask-button"
          disabled={busy || disabled || empty}
          onClick={() => void submit(false)}
        >
          Add to batch
        </button>
      </div>
    </div>
  );
}
