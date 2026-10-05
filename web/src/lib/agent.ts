import type { SelectedLineRange } from "@pierre/diffs";
import { OUTSIDE_DIFF_HINT, clampSelection } from "./review";
import type {
  AgentBatchState,
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

/** What the page knows about the agent beyond the threads: its status and answers in flight. */
export interface AgentLive {
  status: AgentStatus | null;
  /** Answer text streamed so far, by thread id, until the final message arrives. */
  streams: Readonly<Record<string, string>>;
  /** Why the agent did not answer a thread, by thread id, until the thread is sent again. */
  errors: Readonly<Record<string, string>>;
  /** Threads sent to the agent that no status lists in a batch yet. */
  sent: Readonly<Record<string, true>>;
}

export const EMPTY_LIVE: AgentLive = { status: null, streams: {}, errors: {}, sent: {} };

const without = <T>(record: Readonly<Record<string, T>>, key: string): Record<string, T> => {
  if (!(key in record)) return record;
  const { [key]: _removed, ...rest } = record;
  return rest;
};

const TERMINAL_BATCH_STATES: ReadonlySet<AgentBatchState> = new Set(["done", "stopped", "error"]);

/** True while a batch is out with the agent: queued or running. */
export const batchActive = (status: AgentStatus | null) =>
  status !== null && status.batch !== null && !TERMINAL_BATCH_STATES.has(status.batch.state);

/** True when the batch out with the agent carries `threadId` and has not answered it yet. */
function awaitingAnswer(status: AgentStatus | null, threadId: string): boolean {
  const batch = status?.batch ?? null;
  return (
    batch !== null &&
    !TERMINAL_BATCH_STATES.has(batch.state) &&
    batch.thread_ids.includes(threadId) &&
    !batch.answered_ids.includes(threadId)
  );
}

/** True when `threadId` is the overview the warm-up is writing now. */
const warmingUp = (status: AgentStatus, threadId: string) =>
  status.warmup.status === "running" && status.warmup.thread_id === threadId;

export function statusFrom(event: AgentStatus): AgentStatus {
  const batch = event.batch ?? null;
  const partial = event.partial ?? null;
  return {
    state: event.state,
    session_id: event.session_id ?? null,
    model: event.model ?? null,
    cost_usd: event.cost_usd ?? null,
    context_tokens: event.context_tokens ?? null,
    staged_count: event.staged_count ?? 0,
    batch:
      batch === null
        ? null
        : {
            id: batch.id,
            thread_ids: batch.thread_ids ?? [],
            answered_ids: batch.answered_ids ?? [],
            state: batch.state ?? "running",
          },
    partial: partial === null ? null : { thread_id: partial.thread_id, text: partial.text ?? "" },
    warmup: event.warmup ?? { status: "none", thread_id: null },
  };
}

/**
 * Apply an agent status, as GET /agent or an agent_status event carries it. A new batch
 * clears the old errors of the threads it carries, and every thread it lists stops
 * counting as sent but unlisted. When a batch ends (done, stopped, or error), the partial
 * answers of its threads are dropped: a stopped answer that never finished does not stay
 * on screen. The status's `partial` fills in the answer text for a thread still being
 * answered, unless the stream already holds more text than it.
 */
export function withStatus(live: AgentLive, status: AgentStatus): AgentLive {
  let { errors, sent, streams } = live;
  const previous = live.status?.batch ?? null;
  if (previous !== null && batchActive(live.status) && !batchActive(status)) {
    for (const id of previous.thread_ids) streams = without(streams, id);
  }
  const batch = status.batch;
  if (batch !== null) {
    const isNew = previous?.id !== batch.id;
    for (const id of batch.thread_ids) {
      sent = without(sent, id);
      if (isNew) errors = without(errors, id);
    }
  }
  const partial = status.partial;
  if (
    partial !== null &&
    (awaitingAnswer(status, partial.thread_id) || warmingUp(status, partial.thread_id)) &&
    partial.text.length >= (streams[partial.thread_id] ?? "").length
  ) {
    streams = { ...streams, [partial.thread_id]: partial.text };
  }
  return { ...live, status, errors, sent, streams };
}

/** Record that the threads were sent to the agent, clearing their old errors. */
export function markSent(live: AgentLive, threadIds: readonly string[]): AgentLive {
  let { errors } = live;
  const sent = { ...live.sent };
  for (const id of threadIds) {
    errors = without(errors, id);
    sent[id] = true;
  }
  return { ...live, errors, sent };
}

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
        sent: without(live.sent, event.thread_id),
      };
    case "agent_message":
      return {
        ...live,
        streams: without(live.streams, event.thread_id),
        errors: without(live.errors, event.thread_id),
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
 * overview's first message is filled in this way), any other is appended. The thread's
 * `agent_error` clears. Returns null when the thread is not in the list.
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
      agent_error: null,
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

export const isStaged = (message: ThreadMessage) => message.status === "staged";

export const stagedMessages = (thread: ReviewThread) => thread.messages.filter(isStaged);

/**
 * The question threads with something staged. One thread counts once, however many staged
 * messages it holds, as one batch answers each thread once.
 */
export const stagedThreadIds = (threads: readonly ReviewThread[]): string[] =>
  threads.filter((t) => isQuestion(t) && stagedMessages(t).length > 0).map((t) => t.id);

/** A staged message whose edit and delete act on the whole thread: the unsent first question. */
export const ownsThread = (thread: ReviewThread, message: ThreadMessage) =>
  isStaged(message) && thread.messages[0]?.id === message.id;

const mapThread = (
  threads: readonly ReviewThread[],
  threadId: string,
  change: (thread: ReviewThread) => ReviewThread,
): readonly ReviewThread[] => threads.map((t) => (t.id === threadId ? change(t) : t));

/** The threads after a send: their staged messages count as sent and their old error clears. */
export function markThreadsSent(
  threads: readonly ReviewThread[],
  threadIds: readonly string[],
): readonly ReviewThread[] {
  const ids = new Set(threadIds);
  return threads.map((thread) =>
    ids.has(thread.id)
      ? {
          ...thread,
          agent_error: null,
          messages: thread.messages.map((m) =>
            isStaged(m) ? { ...m, status: "sent" as const } : m,
          ),
        }
      : thread,
  );
}

/** Append a staged follow-up to a question thread. */
export const addStagedMessage = (
  threads: readonly ReviewThread[],
  threadId: string,
  message: ThreadMessage,
) => mapThread(threads, threadId, (t) => ({ ...t, messages: [...t.messages, message] }));

/** Replace the body of one message. */
export const editMessageBody = (
  threads: readonly ReviewThread[],
  threadId: string,
  messageId: string,
  body: string,
) =>
  mapThread(threads, threadId, (t) => ({
    ...t,
    messages: t.messages.map((m) => (m.id === messageId ? { ...m, body } : m)),
  }));

/** Drop one message from its thread. */
export const removeMessage = (
  threads: readonly ReviewThread[],
  threadId: string,
  messageId: string,
) =>
  mapThread(threads, threadId, (t) => ({
    ...t,
    messages: t.messages.filter((m) => m.id !== messageId),
  }));

export type QuestionState =
  | { kind: "streaming" }
  | { kind: "waiting" }
  | { kind: "staged"; error: string | null }
  | { kind: "error"; error: string }
  | { kind: "answered" }
  | { kind: "unanswered" };

export interface BatchProgress {
  total: number;
  answered: number;
  /** True while the batch waits behind another job, such as the warm-up. */
  queued: boolean;
}

/** How far the batch out with the agent has got; null when none is out. */
export function batchProgress(status: AgentStatus | null): BatchProgress | null {
  const batch = status?.batch ?? null;
  if (batch === null || !batchActive(status)) return null;
  const listed = new Set(batch.thread_ids);
  const answered = batch.answered_ids.filter((id) => listed.has(id)).length;
  return { total: listed.size, answered, queued: batch.state === "queued" };
}

export const batchProgressLabel = (progress: BatchProgress) =>
  progress.queued
    ? `Queued: ${progress.total} question${progress.total === 1 ? "" : "s"}`
    : `Answered ${progress.answered} of ${progress.total}`;

/**
 * Where a question stands. Streaming text wins. A thread the batch out with the agent has
 * not answered yet waits for the agent, as does one just sent, unless this tab saw the
 * agent skip it. A skipped thread goes back to staged and keeps the reason: the
 * agent_error event in this tab, or the thread's `agent_error` after a reload.
 */
export function questionState(thread: ReviewThread, live: AgentLive): QuestionState {
  if (live.streams[thread.id] !== undefined) return { kind: "streaming" };
  const skipped = live.errors[thread.id];
  if (skipped === undefined && (awaitingAnswer(live.status, thread.id) || live.sent[thread.id])) {
    return { kind: "waiting" };
  }
  const error = skipped ?? thread.agent_error ?? null;
  if (stagedMessages(thread).length > 0) return { kind: "staged", error };
  if (error !== null) return { kind: "error", error };
  const last = thread.messages.at(-1);
  if (last?.author === "agent" && last.body.trim() !== "") return { kind: "answered" };
  return { kind: "unanswered" };
}

export function questionStateLabel(state: QuestionState): string {
  switch (state.kind) {
    case "streaming":
      return "answering";
    case "waiting":
      return "waiting for the agent";
    case "staged":
      return state.error === null ? "staged" : "skipped, staged again";
    case "error":
      return "error";
    case "answered":
      return "answered";
    case "unanswered":
      return "no answer";
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
