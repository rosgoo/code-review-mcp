import { describe, expect, it } from "vitest";
import {
  DEFAULT_CONTROLS,
  arrangeInbox,
  authorsOf,
  buildGroups,
  compareItems,
  countPrs,
  matchesQuery,
  parseControls,
  passesFilter,
  repollDelay,
  MAX_REPOLLS,
  REPOLL_MS,
  shellQuote,
  type InboxControls,
  type InboxGroup,
} from "./inbox";
import { formatSince } from "./time";
import type { InboxItem, InboxList } from "./types";

const REPO = "Maybern/maybern";

function pr(number: number, overrides: Partial<InboxItem> = {}): InboxItem {
  return {
    repo: REPO,
    number,
    title: `PR ${number}`,
    url: `https://github.com/${REPO}/pull/${number}`,
    author: "octocat",
    author_is_bot: false,
    is_draft: false,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-05T00:00:00Z",
    base_ref: "master",
    head_ref: `branch-${number}`,
    additions: 10,
    deletions: 2,
    changed_files: 3,
    review_decision: null,
    viewer_review: null,
    labels: [],
    ci_state: "success",
    review_id: null,
    ...overrides,
  };
}

const controls = (overrides: Partial<InboxControls> = {}): InboxControls => ({
  ...DEFAULT_CONTROLS,
  ...overrides,
});

const numbers = (groups: readonly InboxGroup[]) => groups.map((g) => g.map((e) => e.item.number));
const positions = (groups: readonly InboxGroup[]) =>
  groups.map((g) => g.map((e) => `${e.item.number}:${e.position}/${e.size}@${e.depth}`));

/** A chain like the event-DSL stack: each PR's base is the previous PR's head. */
function chain(start: number, length: number, skip: readonly number[] = []): InboxItem[] {
  const items: InboxItem[] = [];
  for (let i = 0; i < length; i++) {
    const number = start + i;
    if (skip.includes(number)) continue;
    items.push(
      pr(number, {
        base_ref: i === 0 ? "master" : `stack-${number - 1}`,
        head_ref: `stack-${number}`,
        created_at: `2026-10-0${1 + (i % 5)}T00:00:00Z`,
        updated_at: `2026-10-05T0${i % 10}:00:00Z`,
      }),
    );
  }
  return items;
}

describe("filters", () => {
  const items = [
    pr(1, { title: "Fix carry rounding", author: "izaak", labels: ["backend"] }),
    pr(2, { title: "Bump deps", author: "dependabot", author_is_bot: true, ci_state: null }),
    pr(3, { title: "WIP fees", is_draft: true, ci_state: "failure", head_ref: "core-123-fees" }),
    pr(4, { title: "Docs", ci_state: "pending" }),
  ];
  const pick = (overrides: Partial<InboxControls>) =>
    items.filter((item) => passesFilter(item, controls(overrides))).map((item) => item.number);

  it("matches text against repo#number, title, author, branches, and labels", () => {
    expect(matchesQuery(items[0]!, "#1 CARRY izaak")).toBe(true);
    expect(matchesQuery(items[0]!, "backend")).toBe(true);
    expect(matchesQuery(items[2]!, "core-123")).toBe(true);
    expect(matchesQuery(items[1]!, "izaak")).toBe(false);
    expect(pick({ query: "  " })).toEqual([1, 2, 3, 4]);
  });

  it("filters by author, CI state, drafts, and bots", () => {
    expect(pick({ author: "izaak" })).toEqual([1]);
    expect(pick({ ci: "failure" })).toEqual([3]);
    expect(pick({ ci: "pending" })).toEqual([4]);
    expect(pick({ ci: "none" })).toEqual([2]);
    expect(pick({ ci: "success" })).toEqual([1]);
    expect(pick({ hideDrafts: true })).toEqual([1, 2, 4]);
    expect(pick({ hideBots: true })).toEqual([1, 3, 4]);
    expect(pick({ hideBots: true, hideDrafts: true, query: "docs" })).toEqual([4]);
  });

  it("lists the authors present, once each, sorted", () => {
    expect(authorsOf([items, [pr(9, { author: "Ann" }), pr(10, { author: null })]])).toEqual([
      "Ann",
      "dependabot",
      "izaak",
      "octocat",
    ]);
  });
});

describe("sorting", () => {
  const items = [
    pr(1, {
      updated_at: "2026-10-03T00:00:00Z",
      created_at: "2026-09-01T00:00:00Z",
      author: "zed",
      additions: 5,
      deletions: 0,
      ci_state: "success",
    }),
    pr(2, {
      updated_at: "2026-10-05T00:00:00Z",
      created_at: "2026-08-01T00:00:00Z",
      author: "amy",
      additions: 500,
      deletions: 20,
      ci_state: null,
    }),
    pr(3, {
      updated_at: "2026-10-04T00:00:00Z",
      created_at: "2026-10-01T00:00:00Z",
      author: "Bob",
      additions: 50,
      deletions: 50,
      ci_state: "failure",
    }),
    pr(4, {
      updated_at: "2026-10-01T00:00:00Z",
      created_at: "2026-09-15T00:00:00Z",
      author: "amy",
      additions: 1,
      deletions: 1,
      ci_state: "pending",
    }),
  ];
  const order = (sort: InboxControls["sort"]) =>
    [...items].sort(compareItems(sort)).map((item) => item.number);

  it("sorts by last update, newest first, by default", () => {
    expect(order("updated")).toEqual([2, 3, 1, 4]);
  });

  it("sorts by creation, author, CI state with failures first, and size", () => {
    expect(order("created")).toEqual([3, 4, 1, 2]);
    expect(order("author")).toEqual([2, 4, 3, 1]);
    expect(order("ci")).toEqual([3, 4, 2, 1]);
    expect(order("size")).toEqual([2, 3, 1, 4]);
  });
});

describe("stacks", () => {
  it("nests a chain in chain order whatever the input order", () => {
    const stack = chain(100, 11);
    const shuffled = [
      stack[5]!,
      stack[10]!,
      stack[0]!,
      pr(7),
      stack[3]!,
      ...stack.slice(6, 10),
      stack[1]!,
      stack[2]!,
      stack[4]!,
    ];

    const groups = buildGroups(shuffled);

    expect(numbers(groups)).toEqual([[100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110], [7]]);
    expect(positions(groups)[0]!.slice(0, 3)).toEqual(["100:1/11@0", "101:2/11@1", "102:3/11@2"]);
    expect(positions(groups)[1]).toEqual(["7:1/1@0"]);
  });

  it("splits a chain where a middle PR is missing", () => {
    const groups = buildGroups(chain(200, 5, [202]));

    expect(numbers(groups)).toEqual([
      [200, 201],
      [203, 204],
    ]);
    expect(positions(groups)).toEqual([
      ["200:1/2@0", "201:2/2@1"],
      ["203:1/2@0", "204:2/2@1"],
    ]);
  });

  it("keeps siblings oldest first and does not link across repos", () => {
    const parent = pr(1, { head_ref: "feature" });
    const younger = pr(3, { base_ref: "feature", created_at: "2026-10-03T00:00:00Z" });
    const older = pr(2, { base_ref: "feature", created_at: "2026-10-02T00:00:00Z" });
    const elsewhere = pr(4, { repo: "Maybern/docs", base_ref: "feature" });

    expect(positions(buildGroups([younger, elsewhere, older, parent]))).toEqual([
      ["4:1/1@0"],
      ["1:1/3@0", "2:2/3@1", "3:3/3@1"],
    ]);
  });

  it("does not loop on a base/head cycle", () => {
    const a = pr(1, { base_ref: "b", head_ref: "a" });
    const b = pr(2, { base_ref: "a", head_ref: "b" });

    expect(numbers(buildGroups([a, b]))).toEqual([[1, 2]]);
  });

  it("keeps a stack together when sorting and filtering", () => {
    const stack = chain(300, 3);
    stack[1] = { ...stack[1]!, ci_state: "failure", is_draft: true };
    const loose = [pr(1, { ci_state: "pending" }), pr(2, { ci_state: "success" })];

    const byCi = arrangeInbox([...loose, ...stack], controls({ sort: "ci" }));
    expect(numbers(byCi)).toEqual([[300, 301, 302], [1], [2]]);

    const noDrafts = arrangeInbox([...loose, ...stack], controls({ hideDrafts: true }));
    expect(positions(noDrafts).find((g) => g[0]!.startsWith("300"))).toEqual([
      "300:1/3@0",
      "302:3/3@2",
    ]);
    expect(countPrs(noDrafts)).toBe(4);
  });

  it("places a stack by its most recently updated PR", () => {
    const stack = chain(400, 3);
    stack[2] = { ...stack[2]!, updated_at: "2026-10-06T00:00:00Z" };
    const newer = pr(1, { updated_at: "2026-10-05T12:00:00Z" });

    expect(numbers(arrangeInbox([newer, ...stack], controls()))).toEqual([[400, 401, 402], [1]]);
  });
});

describe("controls", () => {
  it("restores saved controls and repairs invalid values", () => {
    expect(parseControls(null)).toEqual(DEFAULT_CONTROLS);
    expect(parseControls("not json")).toEqual(DEFAULT_CONTROLS);
    expect(parseControls(JSON.stringify({ sort: "ci", hideBots: true, author: "amy" }))).toEqual({
      ...DEFAULT_CONTROLS,
      sort: "ci",
      hideBots: true,
      author: "amy",
    });
    expect(parseControls(JSON.stringify({ sort: "random", ci: 3, hideDrafts: "yes" }))).toEqual(
      DEFAULT_CONTROLS,
    );
  });
});

describe("formatting", () => {
  const now = Date.parse("2026-10-05T12:00:00Z");

  it("formats compact ages", () => {
    expect(formatSince("2026-10-05T11:55:00Z", now)).toBe("5m");
    expect(formatSince("2026-10-05T09:00:00Z", now)).toBe("3h");
    expect(formatSince("2026-09-29T12:00:00Z", now)).toBe("6d");
    expect(formatSince("2026-08-31T12:00:00Z", now)).toBe("5w");
    expect(formatSince("2026-05-01T12:00:00Z", now)).toBe("5mo");
    expect(formatSince("2024-01-01T00:00:00Z", now)).toBe("2y");
    expect(formatSince("bad", now)).toBe("");
  });

  it("quotes a path for the shell only when needed", () => {
    expect(shellQuote("/Users/me/.code-review-mcp/worktrees/Maybern-maybern-1")).toBe(
      "/Users/me/.code-review-mcp/worktrees/Maybern-maybern-1",
    );
    expect(shellQuote("/tmp/my dir/it's")).toBe(`'/tmp/my dir/it'\\''s'`);
    expect(shellQuote("~/x")).toBe("'~/x'");
  });
});

describe("repollDelay", () => {
  const list = (refreshing: boolean): InboxList => ({
    name: "team",
    total: 0,
    fetched_at: "2026-10-05T00:00:00Z",
    refreshing,
    items: [],
  });

  it("asks again while the daemon refreshes a stale list, up to a limit", () => {
    expect(repollDelay(list(true), 0)).toBe(REPOLL_MS);
    expect(repollDelay(list(true), MAX_REPOLLS - 1)).toBe(REPOLL_MS);
    expect(repollDelay(list(true), MAX_REPOLLS)).toBeNull();
    expect(repollDelay(list(false), 0)).toBeNull();
  });
});
