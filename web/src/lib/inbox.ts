import type { InboxItem, ReviewSummary } from "./types";

export const prLabel = (repo: string, number: number) => `${repo}#${number}`;

/**
 * Case-insensitive match of every whitespace-separated term against the item's
 * repo#number, title, and author. `#123` and `123` both match PR 123.
 */
export function matchesQuery(item: InboxItem, query: string): boolean {
  const haystack = [prLabel(item.repo, item.number), item.title, item.author ?? ""]
    .join(" ")
    .toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((term) => haystack.includes(term));
}

export function filterItems(items: readonly InboxItem[], query: string): InboxItem[] {
  return items.filter((item) => matchesQuery(item, query));
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
