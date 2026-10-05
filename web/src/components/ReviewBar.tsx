import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { api } from "../lib/api";
import { countByStatus, OVERALL_PATH } from "../lib/comments";
import type { Comment } from "../lib/types";

type SubmitState = "idle" | "sending" | "sent" | "empty" | "error";

const BUTTON_TEXT: Record<SubmitState, string> = {
  idle: "Submit",
  sending: "Sending…",
  sent: "Submitted ✓",
  empty: "Nothing to send",
  error: "Failed, retry",
};

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

interface ReviewBarProps {
  reviewId: string;
  comments: readonly Comment[];
  onSubmitted(): Promise<void>;
}

export function ReviewBar({ reviewId, comments, onSubmitted }: ReviewBarProps) {
  const [overall, setOverall] = useState("");
  const [state, setState] = useState<SubmitState>("idle");
  const resetTimer = useRef<number | undefined>(undefined);
  const { drafts, awaiting } = countByStatus(comments);

  useEffect(() => () => window.clearTimeout(resetTimer.current), []);

  function show(next: SubmitState) {
    window.clearTimeout(resetTimer.current);
    setState(next);
    if (next !== "sending") resetTimer.current = window.setTimeout(() => setState("idle"), 2500);
  }

  async function submit() {
    if (state === "sending") return;
    const text = overall.trim();
    if (!text && drafts === 0 && awaiting === 0) {
      show("empty");
      return;
    }
    show("sending");
    try {
      if (text) {
        await api.addComment(reviewId, {
          path: OVERALL_PATH,
          side: "additions",
          line: 0,
          line_content: "",
          body: text,
        });
      }
      await api.submit(reviewId);
      setOverall("");
      await onSubmitted();
      show("sent");
    } catch {
      show("error");
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      void submit();
    }
  }

  return (
    <footer className="review-bar">
      <textarea
        className="overall-input"
        rows={1}
        placeholder="Overall feedback (optional)"
        value={overall}
        onChange={(event) => setOverall(event.target.value)}
        onKeyDown={onKeyDown}
      />
      <div className="review-bar-status" aria-live="polite">
        <span className={drafts > 0 ? "pill pill-draft" : "pill"}>{plural(drafts, "draft")}</span>
        {awaiting > 0 && <span className="pill pill-submitted">{awaiting} waiting on agent</span>}
      </div>
      <button
        type="button"
        className={`button primary submit-button submit-${state}`}
        onClick={() => void submit()}
        disabled={state === "sending"}
      >
        {BUTTON_TEXT[state]}
      </button>
    </footer>
  );
}
