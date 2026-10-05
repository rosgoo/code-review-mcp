import type {
  CheckState,
  InboxItem,
  InboxList,
  InboxName,
  ReviewSummary,
  StackSource,
} from "./types";

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

export interface GroupEntry {
  item: InboxItem;
  /** 1-based place in the stack; null for a PR based on the stack but not in it. */
  position: number | null;
  /** The PR this one is based on, for a PR based on a GitHub stack but not in it. */
  basedOn: number | null;
}

export interface InboxGroup {
  key: string;
  /** "github" for a GitHub stack, "branches" for a chain of branches, null for a lone PR. */
  source: StackSource | null;
  stackNumber: number | null;
  /** PRs in the whole stack, counting ones this list does not hold. */
  size: number;
  /** Stack entries in position order, then the PRs based on the stack. */
  entries: GroupEntry[];
}

const refKey = (repo: string, ref: string) => `${repo}\u0000${ref}`;
const byCreated = (a: InboxItem, b: InboxItem) =>
  a.created_at.localeCompare(b.created_at) || a.number - b.number;

/**
 * Group PRs into stacks. A PR in a GitHub stack joins the group for that stack number,
 * at its stack position. A PR in no GitHub stack whose base branch is another listed
 * PR's head branch in the same repo is based on that PR: when the chain below it reaches
 * a GitHub stack, it joins that stack's group after the entries; otherwise it nests in a
 * branch chain (parent before child, siblings oldest first). A PR whose parent is not
 * listed starts its own chain, so a missing middle PR splits a chain in two.
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
  const nativeBelow = (item: InboxItem): InboxItem | null => {
    const seen = new Set<InboxItem>([item]);
    for (let parent = parentOf(item); parent !== null; parent = parentOf(parent)) {
      if (parent.stack) return parent;
      if (seen.has(parent)) return null;
      seen.add(parent);
    }
    return null;
  };
  const children = new Map<InboxItem, InboxItem[]>();
  for (const item of items) {
    const parent = item.stack ? null : parentOf(item);
    if (parent !== null) children.set(parent, [...(children.get(parent) ?? []), item]);
  }
  const childrenOf = (item: InboxItem) => [...(children.get(item) ?? [])].sort(byCreated);

  const groupOf = new Map<InboxItem, InboxGroup>();
  const native = new Map<string, InboxGroup>();
  for (const item of items) {
    const stack = item.stack;
    if (!stack) continue;
    const key = `${item.repo}#stack/${stack.number}`;
    let group = native.get(key);
    if (group === undefined) {
      group = { key, source: "github", stackNumber: stack.number, size: stack.size, entries: [] };
      native.set(key, group);
    }
    group.entries.push({ item, position: stack.position, basedOn: null });
    groupOf.set(item, group);
  }
  for (const group of native.values()) {
    group.entries.sort(
      (a, b) => (a.position ?? 0) - (b.position ?? 0) || a.item.number - b.item.number,
    );
    const extend = (base: InboxItem) => {
      for (const child of childrenOf(base)) {
        if (groupOf.has(child)) continue;
        group.entries.push({ item: child, position: null, basedOn: base.number });
        groupOf.set(child, group);
        extend(child);
      }
    };
    for (const entry of [...group.entries]) extend(entry.item);
  }

  const chained = items.filter((item) => !groupOf.has(item) && nativeBelow(item) === null);
  const walk = (root: InboxItem) => {
    const members: InboxItem[] = [];
    const visit = (item: InboxItem) => {
      if (groupOf.has(item) || members.includes(item)) return;
      members.push(item);
      for (const child of childrenOf(item)) visit(child);
    };
    visit(root);
    const group: InboxGroup = {
      key: `${root.repo}#${members.length > 1 ? "branch/" : ""}${root.number}`,
      source: members.length > 1 ? "branches" : null,
      stackNumber: null,
      size: members.length,
      entries: members.map((item, index) => ({ item, position: index + 1, basedOn: null })),
    };
    for (const item of members) groupOf.set(item, group);
  };
  for (const item of chained) if (parentOf(item) === null) walk(item);
  for (const item of chained) if (!groupOf.has(item)) walk(item);
  for (const item of items) {
    if (!groupOf.has(item)) {
      groupOf.set(item, {
        key: `${item.repo}#${item.number}`,
        source: null,
        stackNumber: null,
        size: 1,
        entries: [{ item, position: 1, basedOn: null }],
      });
    }
  }

  const index = new Map(items.map((item, i) => [item, i]));
  const firstIndex = (group: InboxGroup) => index.get(group.entries[0]!.item) ?? 0;
  return [...new Set(groupOf.values())].sort((a, b) => firstIndex(a) - firstIndex(b));
}

/**
 * Stacks from all items, then the filter per PR (positions keep their place in the full
 * stack), then groups sorted by their best-ranked visible PR. A stack stays together.
 */
export function arrangeInbox(items: readonly InboxItem[], controls: InboxControls): InboxGroup[] {
  const compare = compareItems(controls.sort);
  const visible = buildGroups(items)
    .map((group) => ({
      ...group,
      entries: group.entries.filter((entry) => passesFilter(entry.item, controls)),
    }))
    .filter((group) => group.entries.length > 0);
  const best = (group: InboxGroup) =>
    group.entries.map((entry) => entry.item).reduce((a, b) => (compare(a, b) <= 0 ? a : b));
  return visible
    .map((group, index) => ({ group, index, best: best(group) }))
    .sort((a, b) => compare(a.best, b.best) || a.index - b.index)
    .map(({ group }) => group);
}

export const countPrs = (groups: readonly InboxGroup[]) =>
  groups.reduce((count, group) => count + group.entries.length, 0);

const CI_SEVERITY: Record<CheckState, number> = { failure: 0, pending: 1, success: 2, skipped: 3 };

/** The most severe CI state: failure, then pending, then success, then skipped. No CI is ignored. */
export function worstCi(states: readonly (CheckState | null)[]): CheckState | null {
  let worst: CheckState | null = null;
  for (const state of states) {
    if (state !== null && (worst === null || CI_SEVERITY[state] < CI_SEVERITY[worst])) worst = state;
  }
  return worst;
}

const TITLE_TAIL = /[\s:;,|/(\-–—·]+$/;
const OPENERS = "([{";
const CLOSERS = ")]}";

/** `text` up to its first bracket or backtick that does not close. */
function cutUnclosed(text: string): string {
  const open: number[] = [];
  let tick: number | null = null;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i]!;
    if (ch === "`") tick = tick === null ? i : null;
    else if (OPENERS.includes(ch)) open.push(i);
    else if (CLOSERS.includes(ch)) open.pop();
  }
  const cut = Math.min(open[0] ?? text.length, tick ?? text.length);
  return text.slice(0, cut);
}

/**
 * The words every title starts with, without trailing separators or an unclosed
 * bracket, when that is at least two words. Otherwise null.
 */
export function commonTitle(titles: readonly string[]): string | null {
  const [first, ...rest] = titles;
  if (first === undefined) return null;
  let length = first.length;
  for (const title of rest) {
    let i = 0;
    while (i < length && i < title.length && title[i] === first[i]) i++;
    length = i;
  }
  const atBoundary = titles.every((title) => title.length === length || /\s/.test(title[length] ?? ""));
  let prefix = first.slice(0, length);
  if (!atBoundary) prefix = prefix.slice(0, Math.max(0, prefix.search(/\s\S*$/)));
  prefix = cutUnclosed(prefix).replace(TITLE_TAIL, "");
  return prefix.split(/\s+/).filter(Boolean).length >= 2 ? prefix : null;
}

export interface GroupSummary {
  /** The common start of the titles, or the first PR's title. */
  title: string;
  rootTitle: string;
  ci: CheckState | null;
  updatedAt: string;
  authors: string[];
  additions: number;
  deletions: number;
  changedFiles: number;
  /** Shown PRs that are stack entries. */
  inStack: number;
  /** Shown PRs based on the stack but not in it. */
  basedOn: number;
}

export function summarizeGroup(group: InboxGroup): GroupSummary {
  const items = group.entries.map((entry) => entry.item);
  const rootTitle = items[0]?.title ?? "";
  const authors: string[] = [];
  for (const item of items) if (item.author && !authors.includes(item.author)) authors.push(item.author);
  return {
    title: commonTitle(items.map((item) => item.title)) ?? rootTitle,
    rootTitle,
    ci: worstCi(items.map((item) => item.ci_state)),
    updatedAt: items.map((item) => item.updated_at).reduce((a, b) => (b > a ? b : a), ""),
    authors,
    additions: items.reduce((sum, item) => sum + item.additions, 0),
    deletions: items.reduce((sum, item) => sum + item.deletions, 0),
    changedFiles: items.reduce((sum, item) => sum + item.changed_files, 0),
    inStack: group.entries.filter((entry) => entry.position !== null).length,
    basedOn: group.entries.filter((entry) => entry.basedOn !== null).length,
  };
}

/** Saved expanded/collapsed choices, keyed by inbox list and group. */
export type StackExpansion = ReadonlyMap<string, boolean>;

export const MAX_SAVED_EXPANSIONS = 200;

const expansionKey = (list: InboxName, groupKey: string) => `${list}|${groupKey}`;

export function parseExpansion(raw: string | null): StackExpansion {
  let saved: unknown;
  try {
    saved = raw === null ? null : JSON.parse(raw);
  } catch {
    saved = null;
  }
  if (saved === null || typeof saved !== "object" || Array.isArray(saved)) return new Map();
  return new Map(
    Object.entries(saved as Record<string, unknown>).filter(
      (pair): pair is [string, boolean] => typeof pair[1] === "boolean",
    ),
  );
}

export const serializeExpansion = (expansion: StackExpansion) =>
  JSON.stringify(Object.fromEntries(expansion));

/** A stack starts expanded in "mine" and collapsed in the other lists. */
export const isExpanded = (expansion: StackExpansion, list: InboxName, groupKey: string) =>
  expansion.get(expansionKey(list, groupKey)) ?? list === "mine";

/** Record a choice as the newest, keeping the newest MAX_SAVED_EXPANSIONS. */
export function withExpansion(
  expansion: StackExpansion,
  list: InboxName,
  groupKey: string,
  expanded: boolean,
): StackExpansion {
  const next = new Map(expansion);
  const key = expansionKey(list, groupKey);
  next.delete(key);
  next.set(key, expanded);
  for (const old of next.keys()) {
    if (next.size <= MAX_SAVED_EXPANSIONS) break;
    next.delete(old);
  }
  return next;
}

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
