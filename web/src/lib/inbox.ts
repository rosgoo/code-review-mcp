import type { InboxItem, InboxList, InboxName, ReviewSummary } from "./types";

export const INBOX_NAMES: readonly InboxName[] = ["direct", "mine", "team"];

export const REPOLL_MS = 3000;
export const MAX_REPOLLS = 20;

/**
 * When the daemon answered with a stale list it is refreshing in the background, ask
 * again after REPOLL_MS, up to MAX_REPOLLS times in a row. Null means stop.
 */
export function repollDelay(list: InboxList, attempt: number): number | null {
  return list.refreshing && attempt < MAX_REPOLLS ? REPOLL_MS : null;
}

export const prLabel = (repo: string, number: number) => `${repo}#${number}`;

/**
 * Case-insensitive match of every whitespace-separated term against the item's
 * repo#number, title, author, branch names, and labels. `#123` and `123` both match PR 123.
 */
export function matchesQuery(item: InboxItem, query: string): boolean {
  const haystack = [
    prLabel(item.repo, item.number),
    item.title,
    item.author ?? "",
    item.head_ref,
    item.base_ref,
    ...item.labels,
  ]
    .join(" ")
    .toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((term) => haystack.includes(term));
}

export const SORT_KEYS = ["updated", "created", "author", "ci", "size"] as const;
export type SortKey = (typeof SORT_KEYS)[number];

export const CI_FILTERS = ["any", "failure", "pending", "success", "none"] as const;
export type CiFilter = (typeof CI_FILTERS)[number];

export interface InboxControls {
  sort: SortKey;
  query: string;
  author: string;
  ci: CiFilter;
  hideDrafts: boolean;
  hideBots: boolean;
}

export const DEFAULT_CONTROLS: InboxControls = {
  sort: "updated",
  query: "",
  author: "",
  ci: "any",
  hideDrafts: false,
  hideBots: false,
};

const isOneOf = <T extends string>(values: readonly T[], value: unknown): value is T =>
  typeof value === "string" && (values as readonly string[]).includes(value);

/** Read saved controls, falling back to the default for anything missing or invalid. */
export function parseControls(raw: string | null): InboxControls {
  let saved: unknown;
  try {
    saved = raw === null ? null : JSON.parse(raw);
  } catch {
    saved = null;
  }
  if (saved === null || typeof saved !== "object") return { ...DEFAULT_CONTROLS };
  const value = saved as Record<string, unknown>;
  return {
    sort: isOneOf(SORT_KEYS, value.sort) ? value.sort : DEFAULT_CONTROLS.sort,
    query: typeof value.query === "string" ? value.query : "",
    author: typeof value.author === "string" ? value.author : "",
    ci: isOneOf(CI_FILTERS, value.ci) ? value.ci : DEFAULT_CONTROLS.ci,
    hideDrafts: value.hideDrafts === true,
    hideBots: value.hideBots === true,
  };
}

export function passesFilter(item: InboxItem, controls: InboxControls): boolean {
  if (controls.hideDrafts && item.is_draft) return false;
  if (controls.hideBots && item.author_is_bot) return false;
  if (controls.author && item.author !== controls.author) return false;
  if (controls.ci !== "any" && (item.ci_state ?? "none") !== controls.ci) return false;
  return matchesQuery(item, controls.query);
}

const CI_RANK: Record<string, number> = { failure: 0, pending: 1, none: 2, success: 3, skipped: 3 };

const newestFirst = (a: string, b: string) => b.localeCompare(a);

/** Order for a sort key; ties fall back to the most recent update. */
export function compareItems(sort: SortKey): (a: InboxItem, b: InboxItem) => number {
  const byUpdated = (a: InboxItem, b: InboxItem) => newestFirst(a.updated_at, b.updated_at);
  const primary: Record<SortKey, (a: InboxItem, b: InboxItem) => number> = {
    updated: () => 0,
    created: (a, b) => newestFirst(a.created_at, b.created_at),
    author: (a, b) => (a.author ?? "").toLowerCase().localeCompare((b.author ?? "").toLowerCase()),
    ci: (a, b) => (CI_RANK[a.ci_state ?? "none"] ?? 2) - (CI_RANK[b.ci_state ?? "none"] ?? 2),
    size: (a, b) => b.additions + b.deletions - (a.additions + a.deletions),
  };
  return (a, b) => primary[sort](a, b) || byUpdated(a, b);
}

export interface StackEntry {
  item: InboxItem;
  depth: number;
  /** 1-based place in the stack's chain order. */
  position: number;
  /** PRs in the stack; 1 for a PR that is not part of a stack. */
  size: number;
}

export type InboxGroup = StackEntry[];

const refKey = (repo: string, ref: string) => `${repo}\u0000${ref}`;
const byCreated = (a: InboxItem, b: InboxItem) =>
  a.created_at.localeCompare(b.created_at) || a.number - b.number;

/**
 * Group PRs into stacks. A PR whose base branch is another listed PR's head branch in
 * the same repo nests under that PR. Each group lists its PRs in chain order (parent
 * before child, siblings oldest first). A PR whose parent is not listed starts its own
 * group, so a missing middle PR splits a chain in two.
 */
export function buildGroups(items: readonly InboxItem[]): InboxGroup[] {
  const byHead = new Map<string, InboxItem>();
  for (const item of items) {
    const key = refKey(item.repo, item.head_ref);
    if (!byHead.has(key)) byHead.set(key, item);
  }
  const parentOf = (item: InboxItem) => {
    const parent = byHead.get(refKey(item.repo, item.base_ref));
    return parent === undefined || parent === item ? null : parent;
  };
  const children = new Map<InboxItem, InboxItem[]>();
  for (const item of items) {
    const parent = parentOf(item);
    if (parent !== null) children.set(parent, [...(children.get(parent) ?? []), item]);
  }

  const visited = new Set<InboxItem>();
  const groups: InboxGroup[] = [];
  const walk = (root: InboxItem) => {
    const entries: StackEntry[] = [];
    const visit = (item: InboxItem, depth: number) => {
      if (visited.has(item)) return;
      visited.add(item);
      entries.push({ item, depth, position: 0, size: 0 });
      for (const child of [...(children.get(item) ?? [])].sort(byCreated)) visit(child, depth + 1);
    };
    visit(root, 0);
    groups.push(
      entries.map((entry, index) => ({ ...entry, position: index + 1, size: entries.length })),
    );
  };
  for (const item of items) if (parentOf(item) === null) walk(item);
  for (const item of items) if (!visited.has(item)) walk(item);
  return groups;
}

/**
 * Stacks from all items, then the filter per PR (positions keep their place in the full
 * chain), then groups sorted by their best-ranked visible PR. A stack stays together.
 */
export function arrangeInbox(items: readonly InboxItem[], controls: InboxControls): InboxGroup[] {
  const compare = compareItems(controls.sort);
  const visible = buildGroups(items)
    .map((group) => group.filter((entry) => passesFilter(entry.item, controls)))
    .filter((group) => group.length > 0);
  const best = (group: InboxGroup) =>
    group.map((entry) => entry.item).reduce((a, b) => (compare(a, b) <= 0 ? a : b));
  return visible
    .map((group, index) => ({ group, index, best: best(group) }))
    .sort((a, b) => compare(a.best, b.best) || a.index - b.index)
    .map(({ group }) => group);
}

export const countPrs = (groups: readonly InboxGroup[]) =>
  groups.reduce((count, group) => count + group.length, 0);

export function authorsOf(lists: readonly (readonly InboxItem[])[]): string[] {
  const authors = new Set<string>();
  for (const list of lists) for (const item of list) if (item.author) authors.add(item.author);
  return [...authors].sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
}

export interface RecentLabel {
  primary: string;
  secondary: string | null;
}

/** A PR review leads with repo#number and shows its title second; a local review shows its title. */
export function recentLabel(review: ReviewSummary): RecentLabel {
  if (review.kind === "pr" && review.repo !== null && review.pr_number !== null) {
    return { primary: prLabel(review.repo, review.pr_number), secondary: review.title };
  }
  return { primary: review.title, secondary: null };
}

/** Quote `value` for a POSIX shell when it holds characters the shell would split or expand. */
export function shellQuote(value: string): string {
  if (/^[A-Za-z0-9_@%+=:,./-]+$/.test(value)) return value;
  return `'${value.replace(/'/g, `'\\''`)}'`;
}
