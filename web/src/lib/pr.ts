import type { CheckState, FileStatus, PrCheck, PrFile } from "./types";

const STATUS_LETTERS: Record<FileStatus, string> = {
  added: "A",
  modified: "M",
  deleted: "D",
  renamed: "R",
  copied: "C",
  type_changed: "T",
  unmerged: "U",
  unknown: "?",
};

export const statusLetter = (status: FileStatus) => STATUS_LETTERS[status];

export type CollapseOverrides = ReadonlyMap<string, boolean>;

/** A file follows its viewed state (viewed files collapse) unless the user toggled it. */
export function isCollapsed(file: Pick<PrFile, "path" | "viewed">, overrides: CollapseOverrides) {
  return overrides.get(file.path) ?? file.viewed;
}

export function toggleCollapsed(
  overrides: CollapseOverrides,
  file: Pick<PrFile, "path" | "viewed">,
): Map<string, boolean> {
  return new Map(overrides).set(file.path, !isCollapsed(file, overrides));
}

/** Drop the user's toggle so the file collapses or expands with its new viewed state. */
export function followViewed(overrides: CollapseOverrides, path: string): Map<string, boolean> {
  const next = new Map(overrides);
  next.delete(path);
  return next;
}

export function setFileViewed(files: readonly PrFile[], path: string, viewed: boolean): PrFile[] {
  return files.map((file) => (file.path === path ? { ...file, viewed } : file));
}

const LINE_HEIGHT_PX = 20;
const MIN_PLACEHOLDER_PX = 48;
const MAX_PLACEHOLDER_PX = 4000;

/**
 * A rough height for a file's diff before its contents load, so cards far below the
 * visible area stay out of reach of the lazy loader.
 */
export function estimatedDiffHeight(file: Pick<PrFile, "additions" | "deletions" | "binary">) {
  if (file.binary) return MIN_PLACEHOLDER_PX;
  const lines = (file.additions ?? 0) + (file.deletions ?? 0);
  return Math.min(MAX_PLACEHOLDER_PX, Math.max(MIN_PLACEHOLDER_PX, lines * LINE_HEIGHT_PX));
}

export const loadKey = (head: string, path: string) => `${head}:${path}`;

/** Keep only the entries loaded for `head` (none when null). Returns `entries` when nothing is dropped. */
export function keepHead<V>(entries: ReadonlyMap<string, V>, head: string | null): ReadonlyMap<string, V> {
  const prefix = head === null ? null : `${head}:`;
  const kept = [...entries].filter(([key]) => prefix !== null && key.startsWith(prefix));
  return kept.length === entries.size ? entries : new Map(kept);
}

export interface HeadMovedEvent {
  old_head_sha: string | null;
  new_head_sha: string;
}

export interface HeadMovedBanner {
  from: string;
  to: string;
}

/**
 * The "new commits" banner, from the head the page shows to the newest head an event
 * reported. Null when nothing moved past the shown head (for example after a reload).
 */
export function headMovedBanner(
  shownHead: string | null,
  latest: HeadMovedEvent | null,
): HeadMovedBanner | null {
  if (shownHead === null || latest === null || latest.new_head_sha === shownHead) return null;
  return { from: shownHead.slice(0, 7), to: latest.new_head_sha.slice(0, 7) };
}

export type CheckCounts = Record<CheckState, number>;

export function countChecks(checks: readonly PrCheck[]): CheckCounts {
  const counts: CheckCounts = { failure: 0, pending: 0, success: 0, skipped: 0 };
  for (const check of checks) counts[check.state] += 1;
  return counts;
}

/** Failing checks first, then pending ones; passing and skipped checks are left out. */
export function attentionChecks(checks: readonly PrCheck[]): PrCheck[] {
  const rank: Partial<Record<CheckState, number>> = { failure: 0, pending: 1 };
  return checks
    .filter((check) => rank[check.state] !== undefined)
    .sort((a, b) => (rank[a.state] ?? 0) - (rank[b.state] ?? 0) || a.name.localeCompare(b.name));
}

const DECISIONS: Record<string, string> = {
  APPROVED: "Approved",
  CHANGES_REQUESTED: "Changes requested",
  REVIEW_REQUIRED: "Review required",
};

export const decisionLabel = (decision: string | null) =>
  decision === null ? null : DECISIONS[decision] ?? decision.toLowerCase().replace(/_/g, " ");

const VIEWER_REVIEWS: Record<string, string> = {
  APPROVED: "You approved",
  CHANGES_REQUESTED: "You requested changes",
  COMMENTED: "You commented",
  DISMISSED: "Your review was dismissed",
  PENDING: "Your review is pending",
};

export const viewerReviewLabel = (state: string | null) =>
  state === null ? null : (VIEWER_REVIEWS[state] ?? state.toLowerCase().replace(/_/g, " "));
