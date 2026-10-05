import type { PrStack, PrStackEntry, PrStackExtension, StackPr } from "./types";

export interface StackPlace {
  /** The reviewed PR's entry, or null when it is not one of the stack's entries. */
  entry: PrStackEntry | null;
  /** The reviewed PR when it is based on the stack but not in it. */
  extension: PrStackExtension | null;
  prev: StackPr | null;
  next: StackPr | null;
}

export const byPosition = (entries: readonly PrStackEntry[]) =>
  [...entries].sort((a, b) => a.position - b.position);

/** The lowest-numbered PR based on PR `number` that is not in the stack. */
const firstBasedOn = (stack: PrStack, number: number): PrStackExtension | null =>
  stack.extensions
    .filter((x) => x.based_on === number)
    .reduce<PrStackExtension | null>((a, b) => (a === null || b.number < a.number ? b : a), null);

/**
 * Where PR `number` sits in `stack`, and the PRs before and after it. The first entry
 * has no prev. The last entry's next is the lowest-numbered PR based on it, or none. An
 * extension has the PR it is based on as prev, and no next. A PR missing from the lists
 * falls back to the stack's position.
 */
export function stackPlace(stack: PrStack, number: number): StackPlace {
  const entries = byPosition(stack.entries);
  const index = entries.findIndex((e) => e.number === number);
  if (index >= 0) {
    return {
      entry: entries[index] ?? null,
      extension: null,
      prev: entries[index - 1] ?? null,
      next: entries[index + 1] ?? firstBasedOn(stack, number),
    };
  }
  const extension = stack.extensions.find((x) => x.number === number) ?? null;
  if (extension !== null) {
    const base =
      entries.find((e) => e.number === extension.based_on) ??
      stack.extensions.find((x) => x.number === extension.based_on) ??
      null;
    return { entry: null, extension, prev: base, next: null };
  }
  const at = (position: number | null) =>
    position === null ? null : entries.find((e) => e.position === position) ?? null;
  const position = stack.position;
  return {
    entry: null,
    extension: null,
    prev: at(position === null ? null : position - 1),
    next: at(position === null ? null : position + 1),
  };
}

const stackName = (stack: PrStack) =>
  stack.source === "github" && stack.number !== null ? `Stack #${stack.number}` : "Branch stack";

const stackRef = (stack: PrStack) =>
  stack.source === "github" && stack.number !== null ? `stack #${stack.number}` : "a branch stack";

const prCount = (count: number) => `${count} PR${count === 1 ? "" : "s"}`;

/** The PRs based on PR `number` that are not in the stack. */
const basedOnCount = (stack: PrStack, number: number) =>
  stack.extensions.filter((x) => x.based_on === number).length;

export function stackLabel(stack: PrStack, place: StackPlace): string {
  if (place.extension !== null) {
    return `Based on #${place.extension.based_on} · on top of ${stackRef(stack)}`;
  }
  const name = stackName(stack);
  const above = place.entry === null ? 0 : basedOnCount(stack, place.entry.number);
  if (stack.size === 1 && above > 0) return `${name} · ${prCount(above)} based on this PR`;
  const position = place.entry?.position ?? stack.position;
  return position === null
    ? `${name} · ${prCount(stack.size)}`
    : `${name} · ${position} of ${stack.size}`;
}

export const stackPrLabel = (pr: StackPr) => `#${pr.number} ${pr.title}`;

/** The tooltip for a prev or next button: its target, or why it has none. */
export function stepTip(stack: PrStack, target: StackPr | null, edge: string): string {
  if (target === null) return edge;
  const outside = stack.extensions.some((x) => x.number === target.number);
  return outside ? `${stackPrLabel(target)} (not in the stack)` : stackPrLabel(target);
}

/** Why there is no next PR. */
export const nextEdge = (place: StackPlace) =>
  place.extension !== null ? "This PR is not in the stack" : "This is the last PR in the stack";

export type StackMove = "prev" | "next";

export interface KeyInput {
  key: string;
  metaKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  isComposing?: boolean;
  defaultPrevented?: boolean;
}

export interface KeyTarget {
  tagName?: string;
  isContentEditable?: boolean;
}

const EDITABLE_TAGS = new Set(["INPUT", "TEXTAREA", "SELECT"]);

const isEditableTarget = (target: KeyTarget | null) =>
  target !== null &&
  (target.isContentEditable === true || EDITABLE_TAGS.has((target.tagName ?? "").toUpperCase()));

/** `[` moves to the previous PR and `]` to the next, unless a modifier is held or a field has focus. */
export function stackKeyMove(event: KeyInput, target: KeyTarget | null): StackMove | null {
  if (event.metaKey || event.ctrlKey || event.altKey) return null;
  if (event.isComposing === true || event.defaultPrevented === true) return null;
  if (event.key !== "[" && event.key !== "]") return null;
  if (isEditableTarget(target)) return null;
  return event.key === "[" ? "prev" : "next";
}
