import type { DiffLineAnnotation, SelectedLineRange } from "@pierre/diffs";
import type { AnnotationData } from "./annotations";
import type {
  Commentable,
  LineRange,
  ReviewAnchor,
  ReviewEvent,
  ReviewEventName,
  ReviewThread,
  Side,
} from "./types";

export const REVIEW_EVENTS: readonly ReviewEventName[] = ["COMMENT", "APPROVE", "REQUEST_CHANGES"];

export const EVENT_LABELS: Record<ReviewEventName, string> = {
  COMMENT: "Comment",
  APPROVE: "Approve",
  REQUEST_CHANGES: "Request changes",
};

export const OUTSIDE_DIFF_HINT =
  "Review comments go on lines inside the diff. Pick a highlighted line with a +.";

const rangeFor = (ranges: readonly LineRange[], line: number): LineRange | null =>
  ranges.find(([start, end]) => line >= start && line <= end) ?? null;

export function isCommentable(commentable: Commentable | undefined, side: Side, line: number) {
  return commentable !== undefined && rangeFor(commentable[side], line) !== null;
}

export type AnchorResult =
  | { ok: true; anchor: ReviewAnchor; clamped: boolean }
  | { ok: false; hint: string };

/**
 * Turn a line selection into a review comment anchor. A same-side selection that leaves
 * the hunk holding its last line (or, failing that, its first line) is clamped to that
 * hunk. A selection with no commentable endpoint is rejected with a hint. A selection
 * that crosses sides needs both endpoints commentable.
 */
export function clampSelection(
  path: string,
  range: SelectedLineRange,
  commentable: Commentable | undefined,
): AnchorResult {
  if (commentable === undefined) return { ok: false, hint: OUTSIDE_DIFF_HINT };
  const startSide = range.side ?? "additions";
  const endSide = range.endSide ?? startSide;
  if (startSide !== endSide) {
    if (
      !isCommentable(commentable, startSide, range.start) ||
      !isCommentable(commentable, endSide, range.end)
    ) {
      return { ok: false, hint: OUTSIDE_DIFF_HINT };
    }
    return {
      ok: true,
      clamped: false,
      anchor: {
        path,
        side: endSide,
        line: range.end,
        start_line: range.start,
        start_side: startSide,
      },
    };
  }
  const first = Math.min(range.start, range.end);
  const last = Math.max(range.start, range.end);
  const ranges = commentable[endSide];
  const hunk = rangeFor(ranges, last) ?? rangeFor(ranges, first);
  if (hunk === null) return { ok: false, hint: OUTSIDE_DIFF_HINT };
  const start = Math.max(first, hunk[0]);
  const end = Math.min(last, hunk[1]);
  const clamped = start !== first || end !== last;
  const anchor: ReviewAnchor =
    start === end
      ? { path, side: endSide, line: end }
      : { path, side: endSide, line: end, start_line: start, start_side: endSide };
  return { ok: true, anchor, clamped };
}

export const isReviewComment = (thread: ReviewThread) => thread.kind === "review_comment";

/** A thread shows on its line when it is a draft or posted comment anchored at the shown head. */
export function showsInline(thread: ReviewThread, headSha: string | null): boolean {
  return (
    (thread.status === "draft" || thread.status === "posted") &&
    thread.line > 0 &&
    (thread.anchor_sha === null || headSha === null || thread.anchor_sha === headSha)
  );
}

export interface PlacedThreads {
  inline: ReviewThread[];
  /** File-level comments, stale drafts, and posted comments from an older head. */
  block: ReviewThread[];
}

export function placeThreads(
  threads: readonly ReviewThread[],
  headSha: string | null,
): Map<string, PlacedThreads> {
  const byPath = new Map<string, PlacedThreads>();
  for (const thread of threads) {
    if (!isReviewComment(thread)) continue;
    const placed = byPath.get(thread.path) ?? { inline: [], block: [] };
    (showsInline(thread, headSha) ? placed.inline : placed.block).push(thread);
    byPath.set(thread.path, placed);
  }
  return byPath;
}

/** One annotation per (side, line) for the inline threads and the open composer. */
export function reviewAnnotations(
  inline: readonly ReviewThread[],
  composer: ReviewAnchor | null,
): DiffLineAnnotation<AnnotationData>[] {
  const groups = new Map<string, DiffLineAnnotation<AnnotationData>>();
  const groupFor = (side: Side, lineNumber: number) => {
    const key = `${side}:${lineNumber}`;
    let group = groups.get(key);
    if (group === undefined) {
      group = { side, lineNumber, metadata: { threadIds: [], composer: false } };
      groups.set(key, group);
    }
    return group;
  };
  for (const thread of inline)
    groupFor(thread.side, thread.line).metadata.threadIds.push(thread.id);
  if (composer !== null && composer.line > 0)
    groupFor(composer.side, composer.line).metadata.composer = true;
  return [...groups.values()];
}

export function countThreads(threads: readonly ReviewThread[]) {
  const reviewComments = threads.filter(isReviewComment);
  return {
    drafts: reviewComments.filter((t) => t.status === "draft").length,
    stale: reviewComments.filter((t) => t.status === "stale").length,
  };
}

/** Drafts and stale threads in file order, then line order: what still needs the user. */
export function pendingThreads(threads: readonly ReviewThread[], fileOrder: readonly string[]) {
  const rank = new Map(fileOrder.map((path, index) => [path, index]));
  return threads
    .filter((t) => isReviewComment(t) && (t.status === "draft" || t.status === "stale"))
    .sort(
      (a, b) =>
        (rank.get(a.path) ?? Infinity) - (rank.get(b.path) ?? Infinity) ||
        a.path.localeCompare(b.path) ||
        a.line - b.line,
    );
}

export const staleBlockLabel = (count: number) =>
  count === 1 ? "1 stale comment blocks submit" : `${count} stale comments block submit`;

export function locationLabel(anchor: Pick<ReviewAnchor, "path" | "line" | "start_line">): string {
  if (anchor.line === 0) return `${anchor.path} (file)`;
  if (anchor.start_line !== undefined && anchor.start_line !== anchor.line) {
    return `${anchor.path}:${anchor.start_line}–${anchor.line}`;
  }
  return `${anchor.path}:${anchor.line}`;
}

export const threadLocation = (thread: ReviewThread) =>
  locationLabel({
    path: thread.path,
    line: thread.line,
    start_line: thread.start_line ?? undefined,
  });

export interface EventButton {
  event: ReviewEventName;
  label: string;
  disabled: boolean;
  reason: string | null;
}

export interface SubmitState {
  allowedEvents: readonly ReviewEventName[] | undefined;
  isAuthor: boolean;
  drafts: number;
  stale: number;
  summary: string;
  busy: boolean;
}

const OWN_PR_REASONS: Partial<Record<ReviewEventName, string>> = {
  APPROVE: "You can't approve your own PR",
  REQUEST_CHANGES: "You can't request changes on your own PR",
};

/** Which submit buttons work, and why the others do not. */
export function eventButtons(state: SubmitState): EventButton[] {
  const allowed = state.allowedEvents ?? REVIEW_EVENTS;
  const hasSummary = state.summary.trim() !== "";
  return REVIEW_EVENTS.map((event) => {
    let reason: string | null = null;
    if (!allowed.includes(event)) {
      reason =
        (state.isAuthor && OWN_PR_REASONS[event]) || "GitHub does not allow this review here";
    } else if (state.stale > 0) {
      reason = "Re-anchor or delete the stale comments first";
    } else if (event === "COMMENT" && !hasSummary && state.drafts === 0) {
      reason = "Write a summary or a comment first";
    } else if (event === "REQUEST_CHANGES" && !hasSummary) {
      reason = "Write a summary to request changes";
    }
    return {
      event,
      label: EVENT_LABELS[event],
      disabled: state.busy || reason !== null,
      reason,
    };
  });
}

export interface ConfirmComment {
  id: string;
  location: string;
  preview: string;
}

export interface ConfirmContent {
  event: ReviewEventName;
  eventLabel: string;
  summary: string;
  comments: ConfirmComment[];
}

const PREVIEW_LINES = 2;
const PREVIEW_CHARS = 160;

export function previewBody(body: string): string {
  const lines = body.trim().split("\n");
  let preview = lines.slice(0, PREVIEW_LINES).join("\n");
  if (preview.length > PREVIEW_CHARS) preview = `${preview.slice(0, PREVIEW_CHARS).trimEnd()}…`;
  else if (lines.length > PREVIEW_LINES) preview = `${preview}…`;
  return preview;
}

/** What the confirm dialog lists before a review goes to GitHub. */
export function confirmContent(
  event: ReviewEventName,
  summary: string,
  drafts: readonly ReviewThread[],
): ConfirmContent {
  return {
    event,
    eventLabel: EVENT_LABELS[event],
    summary: summary.trim(),
    comments: drafts.map((thread) => ({
      id: thread.id,
      location: threadLocation(thread),
      preview: previewBody(thread.messages[0]?.body ?? ""),
    })),
  };
}

/** The stale thread ids a 409 from submit-review carries, if any. */
export function staleIdsFrom(body: unknown): string[] {
  if (body === null || typeof body !== "object" || !("stale_thread_ids" in body)) return [];
  const ids = (body as { stale_thread_ids: unknown }).stale_thread_ids;
  return Array.isArray(ids) ? ids.filter((id): id is string => typeof id === "string") : [];
}

/** True when a thread event does not carry enough to apply, so the threads must be fetched again. */
export function threadEventNeedsRefetch(event: ReviewEvent): boolean {
  switch (event.type) {
    case "thread_added":
    case "thread_updated":
      return event.thread === undefined;
    case "thread_deleted":
      return (event.thread_id ?? event.comment_id) === undefined;
    case "review_submitted":
      return true;
    default:
      return false;
  }
}

/**
 * Apply a thread event to the list. Returns null when the event does not carry enough
 * to apply it (see threadEventNeedsRefetch).
 */
export function applyThreadEvent(
  threads: readonly ReviewThread[],
  event: ReviewEvent,
): readonly ReviewThread[] | null {
  switch (event.type) {
    case "thread_added":
    case "thread_updated": {
      const thread = event.thread;
      if (thread === undefined) return null;
      const index = threads.findIndex((t) => t.id === thread.id);
      if (index === -1) return [...threads, thread];
      return threads.map((t, i) => (i === index ? thread : t));
    }
    case "thread_deleted": {
      const id = event.thread_id ?? event.comment_id;
      return id === undefined ? null : threads.filter((t) => t.id !== id);
    }
    case "threads_stale": {
      const stale = new Set(event.thread_ids);
      return threads.map((t) => (stale.has(t.id) ? { ...t, status: "stale" as const } : t));
    }
    case "review_submitted":
      return null;
    default:
      return threads;
  }
}
