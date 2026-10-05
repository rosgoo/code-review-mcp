import { useState } from "react";
import { batchProgress, batchProgressLabel, formatCost } from "../lib/agent";
import { useAgentLive, usePrCommentActions, usePrCommentState } from "./PrCommentContext";
import { TypingDots } from "./QuestionThread";
import { errorMessage } from "./Thread";

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

/** Staged questions and the send to the agent, or the progress of the batch out with it. */
export function AgentBar({ staged, onShowQuestions }: { staged: number; onShowQuestions(): void }) {
  const live = useAgentLive();
  const actions = usePrCommentActions();
  const { agentOn } = usePrCommentState();
  const [busy, setBusy] = useState<"send" | "stop" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const status = live.status;
  if (status === null) return null;
  const progress = batchProgress(status);

  async function act(kind: "send" | "stop", action: () => Promise<void>) {
    setBusy(kind);
    setError(null);
    try {
      await action();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  const sendButton = (
    <button
      type="button"
      className="button agent-send"
      disabled={busy !== null || !agentOn || staged === 0}
      title={progress !== null ? "Send the staged questions after this batch" : undefined}
      onClick={() => void act("send", () => actions.send())}
    >
      {busy === "send" ? "Sending…" : "Send to agent"}
    </button>
  );
  const stagedPill = (
    <button
      type="button"
      className={staged > 0 ? "pill pill-staged" : "pill"}
      title="Show the questions"
      onClick={onShowQuestions}
    >
      {plural(staged, "question")} staged
    </button>
  );
  return (
    <div className="agent-bar" role="region" aria-label="Questions to the agent">
      <span className="agent-bar-title">Agent</span>
      {progress !== null ? (
        <>
          <span className="agent-bar-progress" aria-live="polite">
            <TypingDots
              label={progress.queued ? "The batch waits for the agent" : "The agent is answering"}
            />{" "}
            {batchProgressLabel(progress)}
          </span>
          <progress max={progress.total} value={progress.answered} />
          {staged > 0 && stagedPill}
          <span className="spacer" />
          <span className="agent-meter">{formatCost(status.cost_usd)}</span>
          {staged > 0 && sendButton}
          <button
            type="button"
            className="button"
            disabled={busy !== null}
            onClick={() => void act("stop", () => actions.stopBatch())}
          >
            Stop
          </button>
        </>
      ) : (
        <>
          {stagedPill}
          {status.state === "off" && <span className="muted">The review agent is off.</span>}
          <span className="spacer" />
          <span className="agent-meter">{formatCost(status.cost_usd)}</span>
          {sendButton}
        </>
      )}
      {error && (
        <span className="form-error" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}
