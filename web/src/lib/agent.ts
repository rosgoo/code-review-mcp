import type { SelectedLineRange } from "@pierre/diffs";
import { OUTSIDE_DIFF_HINT, clampSelection } from "./review";
import type {
  AgentStatus,
  Commentable,
  ComposerMode,
  LineRange,
  ReviewAnchor,
  ReviewEvent,
  ReviewThread,
  Side,
  ThreadMessage,
} from "./types";

/** What the page knows about the agent beyond the threads: its status and turns in flight. */
export interface AgentLive {
  status: AgentStatus | null;
  /** Answer text streamed so far, by thread id, until the final message arrives. */
  streams: Readonly<Record<string, string>>;
  /** The last turn error, by thread id, until the thread gets a new turn. */
  errors: Readonly<Record<string, string>>;
  /** Threads sent to the agent that the status has not listed yet. */
  sent: Readonly<Record<string, true>>;
}

export const EMPTY_LIVE: AgentLive = { status: null, streams: {}, errors: {}, sent: {} };

const without = <T>(record: Readonly<Record<string, T>>, key: string): Record<string, T> => {
  if (!(key in record)) return record;
  const { [key]: _removed, ...rest } = record;
  return rest;
};

export function statusFrom(event: AgentStatus): AgentStatus {
  return {
    state: event.state,
    session_id: event.session_id ?? null,
    model: event.model ?? null,
    cost_usd: event.cost_usd ?? null,
    context_tokens: event.context_tokens ?? null,
    queue: event.queue ?? [],
    running_thread_id: event.running_thread_id ?? null,
    warmup: event.warmup ?? { status: "none", thread_id: null },
  };
}

/** Apply an agent status, as GET /agent or an agent_status event carries it. */
export function withStatus(live: AgentLive, status: AgentStatus): AgentLive {
  const active = new Set(status.queue);
  if (status.running_thread_id !== null) active.add(status.running_thread_id);
  let { errors, sent } = live;
  for (const id of active) {
    errors = without(errors, id);
    sent = without(sent, id);
  }
  return { ...live, status, errors, sent };
}

/** Record that a question or follow-up for `threadId` was sent, clearing its old error. */
export const markSent = (live: AgentLive, threadId: string): AgentLive => ({
  ...live,
  errors: without(live.errors, threadId),
  sent: { ...live.sent, [threadId]: true },
});

export function applyAgentEvent(live: AgentLive, event: ReviewEvent): AgentLive {
  switch (event.type) {
    case "agent_status":
      return withStatus(live, statusFrom(event));
    case "agent_delta":
      return {
        ...live,
        streams: {
          ...live.streams,
          [event.thread_id]: (live.streams[event.thread_id] ?? "") + event.text,
        },
        errors: without(live.errors, event.thread_id),
        sent: without(live.sent, event.thread_id),
      };
    case "agent_message":
      return {
        ...live,
        streams: without(live.streams, event.thread_id),
        sent: without(live.sent, event.thread_id),
      };
    case "agent_error":
      return {
        ...live,
        streams: without(live.streams, event.thread_id),
        errors: { ...live.errors, [event.thread_id]: event.error },
        sent: without(live.sent, event.thread_id),
      };
    default:
      return live;
  }
}

/**
 * Put an agent message into its thread: a message with a known id replaces it (the
 * overview's first message is filled in this way), any other is appended. Returns null
 * when the thread is not in the list.
 */
export function applyAgentMessage(
  threads: readonly ReviewThread[],
  threadId: string,
  message: ThreadMessage,
): readonly ReviewThread[] | null {
  const index = threads.findIndex((t) => t.id === threadId);
  if (index === -1) return null;
  return threads.map((thread, i) => {
    if (i !== index) return thread;
    const known = thread.messages.some((m) => m.id === message.id);
    return {
      ...thread,
      messages: known
        ? thread.messages.map((m) => (m.id === message.id ? message : m))
        : [...thread.messages, message],
    };
  });
}

export const isQuestion = (thread: ReviewThread) => thread.kind === "question";

/** The overview: a PR-level question thread the agent started. */
export const isOverview = (thread: ReviewThread, status: AgentStatus | null) =>
  isQuestion(thread) &&
  thread.path === "" &&
  thread.line === 0 &&
  (thread.created_by === "agent" || status?.warmup.thread_id === thread.id);

export type QuestionState =
  | { kind: "running" }
  | { kind: "queued"; ahead: number }
  | { kind: "sent" }
  | { kind: "error"; error: string }
  | { kind: "answered" }
  | { kind: "waiting" };

/** The turns before `threadId`: the running one, then the queue ahead of it. */
export function turnsAhead(status: AgentStatus, threadId: string): number | null {
  const index = status.queue.indexOf(threadId);
  if (index === -1) return null;
  return index + (status.running_thread_id !== null ? 1 : 0);
}

export const queueLabel = (ahead: number) =>
  ahead === 0 ? "queued: next" : `queued: ${ahead} ahead`;

export function questionState(thread: ReviewThread, live: AgentLive): QuestionState {
  const status = live.status;
  if (status !== null && status.running_thread_id === thread.id) return { kind: "running" };
  const ahead = status === null ? null : turnsAhead(status, thread.id);
  if (ahead !== null) return { kind: "queued", ahead };
  const error = live.errors[thread.id];
  if (error !== undefined) return { kind: "error", error };
  const last = thread.messages.at(-1);
  if (last?.author === "agent" && last.body.trim() !== "") return { kind: "answered" };
  if (live.sent[thread.id]) return { kind: "sent" };
  return { kind: "waiting" };
}

export function questionStateLabel(state: QuestionState): string {
  switch (state.kind) {
    case "running":
      return "answering";
    case "queued":
      return queueLabel(state.ahead);
    case "sent":
      return "sent";
    case "error":
      return "error";
    case "answered":
      return "answered";
    case "waiting":
      return "no answer yet";
  }
}

export function formatCost(usd: number | null): string {
  const value = usd ?? 0;
  if (value > 0 && value < 0.01) return "<$0.01";
  return `$${value.toFixed(2)}`;
}

export function formatTokens(tokens: number | null): string {
  if (tokens === null) return "—";
  if (tokens < 1000) return `${tokens} tokens`;
  if (tokens < 1_000_000) return `${(tokens / 1000).toFixed(1).replace(/\.0$/, "")}k tokens`;
  return `${(tokens / 1_000_000).toFixed(2).replace(/\.?0+$/, "")}M tokens`;
}

/** The composer for one picked line or range, in either mode. */
export interface Composer {
  path: string;
  /** Where a question goes: the lines as picked. */
  question: ReviewAnchor;
  /** Where a review comment goes, clamped to the diff; null outside the diff. */
  comment: ReviewAnchor | null;
  /** True when the comment anchor was trimmed to fit the diff. */
  clamped: boolean;
  mode: ComposerMode;
}

export const composerAnchor = (composer: Composer): ReviewAnchor =>
  composer.mode === "comment" && composer.comment !== null ? composer.comment : composer.question;

/** The picked lines as an anchor: the range in order on one side, as given across sides. */
export function pickedAnchor(path: string, range: SelectedLineRange): ReviewAnchor {
  const side = range.side ?? "additions";
  const endSide = range.endSide ?? side;
  if (side !== endSide) {
    return { path, side: endSide, line: range.end, start_line: range.start, start_side: side };
  }
  const first = Math.min(range.start, range.end);
  const last = Math.max(range.start, range.end);
  return first === last
    ? { path, side, line: last }
    : { path, side, line: last, start_line: first, start_side: side };
}

export type ComposerResult = { ok: true; composer: Composer } | { ok: false; hint: string };

/**
 * Open a composer for the picked lines. A question may go on any line; a review comment
 * only inside the diff. The composer starts in `preferred` mode when that mode can take
 * the lines, and in the other mode when it cannot. With the agent off, only comments.
 */
export function composerFor(
  path: string,
  range: SelectedLineRange,
  commentable: Commentable | undefined,
  preferred: ComposerMode,
  agentOn: boolean,
): ComposerResult {
  const clamped = clampSelection(path, range, commentable);
  const comment = clamped.ok ? clamped.anchor : null;
  if (!agentOn && comment === null) return { ok: false, hint: OUTSIDE_DIFF_HINT };
  const mode: ComposerMode =
    !agentOn || (preferred === "comment" && comment !== null)
      ? "comment"
      : comment === null
        ? "question"
        : preferred;
  return {
    ok: true,
    composer: {
      path,
      question: pickedAnchor(path, range),
      comment,
      clamped: clamped.ok && clamped.clamped,
      mode,
    },
  };
}

/** A composer for the whole file, in `mode`. */
export function fileComposer(path: string, mode: ComposerMode): Composer {
  const anchor: ReviewAnchor = { path, side: "additions", line: 0 };
  return { path, question: anchor, comment: anchor, clamped: false, mode };
}

const inRanges = (ranges: readonly LineRange[], line: number) =>
  ranges.some(([start, end]) => line >= start && line <= end);

/** True when the line shows in the diff without expanding unchanged lines. */
export const inHunks = (commentable: Commentable | undefined, side: Side, line: number) =>
  commentable !== undefined && inRanges(commentable[side], line);

/**
 * Split inline threads into those whose line the diff shows and those it hides in
 * unchanged lines. A hidden thread is listed above the diff instead.
 */
export function splitByHunks(
  inline: readonly ReviewThread[],
  commentable: Commentable | undefined,
): { shown: ReviewThread[]; hidden: ReviewThread[] } {
  const shown: ReviewThread[] = [];
  const hidden: ReviewThread[] = [];
  for (const thread of inline) {
    (commentable === undefined || inHunks(commentable, thread.side, thread.line)
      ? shown
      : hidden
    ).push(thread);
  }
  return { shown, hidden };
}

/** Question threads for the Questions tab: PR-level first, then files in order, then lines. */
export function questionThreads(
  threads: readonly ReviewThread[],
  fileOrder: readonly string[],
  status: AgentStatus | null,
): ReviewThread[] {
  const rank = new Map(fileOrder.map((path, index) => [path, index]));
  const order = (t: ReviewThread) => (t.path === "" ? -1 : rank.get(t.path) ?? Infinity);
  return threads
    .filter((t) => isQuestion(t) && !isOverview(t, status))
    .sort(
      (a, b) =>
        order(a) - order(b) ||
        a.path.localeCompare(b.path) ||
        a.line - b.line ||
        a.created_at.localeCompare(b.created_at),
    );
}

/** The thread location in words: the PR, a file, or a line. */
export function questionLocation(thread: ReviewThread): string {
  if (thread.path === "") return "this PR";
  if (thread.line === 0) return `${thread.path} (file)`;
  if (thread.start_line !== null && thread.start_line !== thread.line) {
    return `${thread.path}:${thread.start_line}–${thread.line}`;
  }
  return `${thread.path}:${thread.line}`;
}
