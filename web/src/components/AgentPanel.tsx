import { useState } from "react";
import { formatCost, formatTokens } from "../lib/agent";
import type { AgentState, ReviewThread } from "../lib/types";
import { Markdown } from "./Markdown";
import { useAgentLive, usePrCommentState } from "./PrCommentContext";
import { AskBox, QuestionThreadCard, TypingDots } from "./QuestionThread";
import { errorMessage } from "./Thread";

const STATE_LABELS: Record<AgentState, string> = {
  idle: "idle",
  queued: "queued",
  running: "answering",
  error: "error",
  off: "off",
};

function Overview({
  overview,
  onWarmup,
}: {
  overview: ReviewThread | null;
  onWarmup(): Promise<void>;
}) {
  const live = useAgentLive();
  const { agentOn, citations } = usePrCommentState();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const warmup = live.status?.warmup.status ?? "none";
  const body = overview?.messages[0]?.body.trim() ?? "";
  const stream = overview === null ? undefined : live.streams[overview.id];
  const failure = overview === null ? undefined : live.errors[overview.id];

  async function generate() {
    setBusy(true);
    setError(null);
    try {
      await onWarmup();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const generateButton = (label: string) =>
    agentOn && (
      <button
        type="button"
        className="button ask-button"
        disabled={busy}
        onClick={() => void generate()}
      >
        {busy ? "Starting…" : label}
      </button>
    );

  let content;
  if (body) {
    content = (
      <details className="agent-overview" open>
        <summary>Overview</summary>
        <Markdown text={body} citations={citations} />
      </details>
    );
  } else if (warmup === "running" || stream !== undefined) {
    content = (
      <div className="agent-overview agent-overview-running">
        <div className="agent-overview-title">
          Writing the overview <TypingDots label="The agent is writing the overview" />
        </div>
        {stream && <Markdown text={stream} citations={citations} />}
      </div>
    );
  } else if (warmup === "error" || failure !== undefined) {
    content = (
      <div className="question-problem" role="alert">
        <span>The overview failed{failure ? `: ${failure}` : "."}</span>
        {generateButton("Try again")}
      </div>
    );
  } else {
    content = (
      <div className="agent-overview-empty">
        <span className="muted">
          The agent can read the diff and write a short overview of this PR.
        </span>
        {generateButton("Generate overview")}
      </div>
    );
  }
  return (
    <>
      {content}
      {error && <p className="form-error">{error}</p>}
    </>
  );
}

/** The review agent at the top of a PR page: its state, the overview, and PR-level questions. */
export function AgentPanel({
  open,
  overview,
  questions,
  onToggle,
  onWarmup,
  onAsk,
}: {
  open: boolean;
  overview: ReviewThread | null;
  questions: readonly ReviewThread[];
  onToggle(): void;
  onWarmup(): Promise<void>;
  onAsk(body: string): Promise<void>;
}) {
  const live = useAgentLive();
  const { agentOn } = usePrCommentState();
  const status = live.status;
  if (status === null) return null;
  const queued = status.queue.length;
  return (
    <section className={open ? "agent-panel" : "agent-panel collapsed"} aria-label="Review agent">
      <header className="agent-panel-header">
        <button
          type="button"
          className="collapse-button"
          aria-expanded={open}
          aria-label={open ? "Collapse the agent panel" : "Expand the agent panel"}
          onClick={onToggle}
        >
          {open ? "▾" : "▸"}
        </button>
        <span className="agent-panel-title">Agent</span>
        <span className={`chip chip-agent-${status.state}`}>
          {STATE_LABELS[status.state]}
          {queued > 0 && ` · ${queued} queued`}
        </span>
        {status.model && <code className="muted agent-model">{status.model}</code>}
        <span className="spacer" />
        <span className="agent-meter" title="What this review's agent session has cost so far">
          {formatCost(status.cost_usd)}
        </span>
        <span className="agent-meter muted" title="Context the agent session holds now">
          {formatTokens(status.context_tokens)}
        </span>
      </header>
      {open && (
        <div className="agent-panel-body">
          {status.state === "off" ? (
            <p className="muted">
              The review agent is off. Turn it on in the daemon config to ask questions.
            </p>
          ) : (
            <>
              <Overview overview={overview} onWarmup={onWarmup} />
              <AskBox disabled={!agentOn} onAsk={onAsk} />
            </>
          )}
          {questions.length > 0 && (
            <div className="agent-questions">
              {questions.map((thread) => (
                <QuestionThreadCard key={thread.id} thread={thread} />
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
