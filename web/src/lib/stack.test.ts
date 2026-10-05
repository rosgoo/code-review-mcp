import { describe, expect, it } from "vitest";
import { nextEdge, stackKeyMove, stackLabel, stackPlace, stepTip, type KeyInput } from "./stack";
import type { PrStack, PrStackEntry, PrStackExtension } from "./types";

const URL_BASE = "https://github.com/Maybern/maybern/pull";

function entry(number: number, position: number): PrStackEntry {
  return {
    position,
    number,
    title: `PR ${number}`,
    state: "open",
    is_draft: false,
    head_ref: `branch-${position}`,
    base_ref: position === 1 ? "master" : `branch-${position - 1}`,
    url: `${URL_BASE}/${number}`,
    ci_state: "success",
    review_id: null,
  };
}

function extension(number: number, basedOn: number): PrStackExtension {
  const { position: _position, ...pr } = entry(number, 99);
  return { ...pr, based_on: basedOn };
}

const NUMBERS = [23897, 23898, 23899, 23900, 23901, 23902, 23903, 23904, 23905, 23925];

function nativeStack(position: number | null = 3): PrStack {
  return {
    source: "github",
    number: 23906,
    size: 10,
    base_ref: "master",
    position,
    entries: NUMBERS.map((number, i) => entry(number, i + 1)).reverse(),
    extensions: [extension(23907, 23925)],
  };
}

const numberOf = (pr: { number: number } | null) => pr?.number ?? null;

describe("stackPlace", () => {
  it("finds the PRs before and after one in the middle, in position order", () => {
    const place = stackPlace(nativeStack(), 23899);

    expect(place.entry?.position).toBe(3);
    expect(numberOf(place.prev)).toBe(23898);
    expect(numberOf(place.next)).toBe(23900);
  });

  it("has no prev at the first entry and no next at a last entry with nothing on it", () => {
    const first = stackPlace(nativeStack(1), 23897);
    const last = stackPlace({ ...nativeStack(10), extensions: [] }, 23925);

    expect(numberOf(first.prev)).toBeNull();
    expect(numberOf(first.next)).toBe(23898);
    expect(numberOf(last.prev)).toBe(23905);
    expect(numberOf(last.next)).toBeNull();
  });

  it("goes from the last entry to the PR based on it, the lowest number first", () => {
    const one = nativeStack(10);
    const several: PrStack = {
      ...nativeStack(10),
      extensions: [extension(24010, 23925), extension(23907, 23925), extension(24001, 23900)],
    };

    expect(numberOf(stackPlace(one, 23925).next)).toBe(23907);
    expect(numberOf(stackPlace(several, 23925).next)).toBe(23907);
    expect(numberOf(stackPlace(several, 23900).next)).toBe(23901);
  });

  it("gives an extension its base as prev and no next", () => {
    const place = stackPlace(nativeStack(null), 23907);

    expect(place.entry).toBeNull();
    expect(place.extension?.based_on).toBe(23925);
    expect(numberOf(place.prev)).toBe(23925);
    expect(numberOf(place.next)).toBeNull();
  });

  it("falls back to the stack position for a PR missing from the lists", () => {
    const place = stackPlace(nativeStack(5), 1);

    expect(numberOf(place.prev)).toBe(23900);
    expect(numberOf(place.next)).toBe(23902);
    expect(stackPlace(nativeStack(null), 1)).toMatchObject({ prev: null, next: null });
  });
});

describe("stackLabel", () => {
  it("names a GitHub stack by number and a branch stack by kind", () => {
    const native = nativeStack();
    const branches: PrStack = { ...nativeStack(3), source: "branches", number: null, size: 7 };

    expect(stackLabel(native, stackPlace(native, 23899))).toBe("Stack #23906 · 3 of 10");
    expect(stackLabel(branches, stackPlace(branches, 23899))).toBe("Branch stack · 3 of 7");
  });

  it("says when the PR is based on the stack but not in it", () => {
    const native = nativeStack(null);

    expect(stackLabel(native, stackPlace(native, 23907))).toBe(
      "Based on #23925 · on top of stack #23906",
    );
    expect(stackLabel(native, stackPlace(native, 1))).toBe("Stack #23906 · 10 PRs");
  });

  it("counts the PRs based on a fork with no parent", () => {
    const fork: PrStack = {
      source: "branches",
      number: null,
      size: 1,
      base_ref: "master",
      position: 1,
      entries: [entry(500, 1)],
      extensions: [extension(501, 500), extension(502, 500)],
    };
    const place = stackPlace(fork, 500);

    expect(stackLabel(fork, place)).toBe("Branch stack · 2 PRs based on this PR");
    expect(numberOf(place.next)).toBe(501);
  });

  it("names the target in the tooltip and marks a PR outside the stack", () => {
    const native = nativeStack(10);
    const plain: PrStack = { ...nativeStack(10), extensions: [] };
    const last = stackPlace(native, 23925);

    expect(stepTip(native, last.next, nextEdge(last))).toBe("#23907 PR 23907 (not in the stack)");
    expect(stepTip(native, last.prev, "edge")).toBe("#23905 PR 23905");
    expect(stepTip(plain, null, nextEdge(stackPlace(plain, 23925)))).toBe(
      "This is the last PR in the stack",
    );
    expect(nextEdge(stackPlace(native, 23907))).toBe("This PR is not in the stack");
  });
});

describe("stackKeyMove", () => {
  const key = (value: string, overrides: Partial<KeyInput> = {}): KeyInput => ({
    key: value,
    metaKey: false,
    ctrlKey: false,
    altKey: false,
    ...overrides,
  });
  const body = { tagName: "BODY", isContentEditable: false };

  it("maps [ to prev and ] to next", () => {
    expect(stackKeyMove(key("["), body)).toBe("prev");
    expect(stackKeyMove(key("]"), body)).toBe("next");
    expect(stackKeyMove(key("]"), null)).toBe("next");
    expect(stackKeyMove(key("]"), { tagName: "button" })).toBe("next");
  });

  it("ignores other keys and modified presses", () => {
    expect(stackKeyMove(key("{"), body)).toBeNull();
    expect(stackKeyMove(key("j"), body)).toBeNull();
    expect(stackKeyMove(key("[", { metaKey: true }), body)).toBeNull();
    expect(stackKeyMove(key("]", { ctrlKey: true }), body)).toBeNull();
    expect(stackKeyMove(key("]", { altKey: true }), body)).toBeNull();
    expect(stackKeyMove(key("]", { isComposing: true }), body)).toBeNull();
    expect(stackKeyMove(key("]", { defaultPrevented: true }), body)).toBeNull();
  });

  it("ignores a press while a field has focus", () => {
    for (const tagName of ["INPUT", "TEXTAREA", "SELECT", "textarea"]) {
      expect(stackKeyMove(key("]"), { tagName })).toBeNull();
    }
    expect(stackKeyMove(key("["), { tagName: "DIV", isContentEditable: true })).toBeNull();
  });
});
