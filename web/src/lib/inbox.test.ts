import { describe, expect, it } from "vitest";
import {
  DEFAULT_CONTROLS,
  MAX_SAVED_EXPANSIONS,
  arrangeInbox,
  authorsOf,
  buildGroups,
  commonTitle,
  compareItems,
  countPrs,
  isExpanded,
  matchesQuery,
  parseControls,
  parseExpansion,
  passesFilter,
  repollDelay,
  MAX_REPOLLS,
  REPOLL_MS,
  serializeExpansion,
  shellQuote,
  summarizeGroup,
  withExpansion,
  worstCi,
  type InboxControls,
  type InboxGroup,
  type StackExpansion,
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
    stack: null,
    ...overrides,
  };
}

const controls = (overrides: Partial<InboxControls> = {}): InboxControls => ({
  ...DEFAULT_CONTROLS,
  ...overrides,
});

const numbers = (groups: readonly InboxGroup[]) =>
  groups.map((g) => g.entries.map((e) => e.item.number));
const positions = (groups: readonly InboxGroup[]) =>
  groups.map((g) =>
    g.entries.map((e) =>
      e.basedOn === null ? `${e.item.number}:${e.position}/${g.size}` : `${e.item.number}^${e.basedOn}`,
    ),
  );

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
    expect(positions(groups)[0]!.slice(0, 3)).toEqual(["100:1/11", "101:2/11", "102:3/11"]);
    expect(positions(groups)[1]).toEqual(["7:1/1"]);
  });

  it("splits a chain where a middle PR is missing", () => {
    const groups = buildGroups(chain(200, 5, [202]));

    expect(numbers(groups)).toEqual([
      [200, 201],
      [203, 204],
    ]);
    expect(positions(groups)).toEqual([
      ["200:1/2", "201:2/2"],
      ["203:1/2", "204:2/2"],
    ]);
  });

  it("keeps siblings oldest first and does not link across repos", () => {
    const parent = pr(1, { head_ref: "feature" });
    const younger = pr(3, { base_ref: "feature", created_at: "2026-10-03T00:00:00Z" });
    const older = pr(2, { base_ref: "feature", created_at: "2026-10-02T00:00:00Z" });
    const elsewhere = pr(4, { repo: "Maybern/docs", base_ref: "feature" });

    expect(positions(buildGroups([younger, elsewhere, older, parent]))).toEqual([
      ["4:1/1"],
      ["1:1/3", "2:2/3", "3:3/3"],
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
      "300:1/3",
      "302:3/3",
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

const DSL_TITLES = [
  "INV-1590: Event DSL 1/10: app tweaks for the event-testing harness",
  "INV-1590: Event DSL 2/10: harness core",
  "INV-1590: Event DSL 3/10: capital activity and fee builders",
  "INV-1590: Event DSL 4/10: allocation rules",
  "INV-1590: Event DSL 5/10: credit facility",
  "INV-1590: Event DSL 6/10: waterfall, reporting and fund structure",
  "INV-1590: Event DSL 7/10: invariants",
  "INV-1590: Event DSL 8/10: commands and the gap gate",
  "INV-1590: Event DSL 9/10: template translator and template diff",
  "INV-1590: Event DSL 10/10: scenarios on a built base",
];
const DSL_NUMBERS = [23897, 23898, 23899, 23900, 23901, 23902, 23903, 23904, 23905, 23925];

/** The event-DSL stack as GitHub stack #23906, plus #23907 based on its last branch. */
function eventDsl(): InboxItem[] {
  const entries = DSL_NUMBERS.map((number, i) =>
    pr(number, {
      title: DSL_TITLES[i]!,
      author: "rosgoo",
      base_ref: i === 0 ? "master" : `ryan/event-dsl-${i}`,
      head_ref: `ryan/event-dsl-${i + 1}`,
      updated_at: `2026-10-0${1 + (i % 5)}T00:00:00Z`,
      stack: { number: 23906, size: 10, position: i + 1 },
    }),
  );
  const dbTests = pr(23907, {
    title: "INV-1590: Event DSL DB tests: calculation reversion and feature flags",
    author: "rosgoo",
    base_ref: "ryan/event-dsl-10",
    head_ref: "ryan/event-dsl-db-tests",
  });
  return [...entries, dbTests];
}

describe("GitHub stacks", () => {
  it("groups by stack number in position order, whatever the input order", () => {
    const items = eventDsl().slice(0, 10);
    const shuffled = [items[4]!, pr(1), items[9]!, items[0]!, ...items.slice(5, 9), ...items.slice(1, 4)];

    const groups = buildGroups(shuffled);

    expect(numbers(groups)).toEqual([[1], [...DSL_NUMBERS]]);
    expect(groups[1]).toMatchObject({ key: `${REPO}#stack/23906`, source: "github", stackNumber: 23906, size: 10 });
    expect(positions(groups)[1]!.slice(0, 2)).toEqual(["23897:1/10", "23898:2/10"]);
    expect(groups[0]).toMatchObject({ source: null, size: 1 });
  });

  it("puts a PR based on the stack after the entries, marked with its base", () => {
    const items = eventDsl();
    const onTop = pr(23999, { base_ref: "ryan/event-dsl-db-tests", head_ref: "ryan/more" });

    const groups = buildGroups([onTop, ...items]);

    expect(groups).toHaveLength(1);
    expect(positions(groups)[0]!.slice(-3)).toEqual(["23925:10/10", "23907^23925", "23999^23907"]);
    expect(groups[0]!.entries.at(-1)).toMatchObject({ position: null, basedOn: 23907 });
  });

  it("keeps the stack size when the list holds only some entries", () => {
    const items = eventDsl();
    const groups = buildGroups([items[2]!, items[5]!]);

    expect(positions(groups)).toEqual([["23899:3/10", "23902:6/10"]]);
  });

  it("falls back to branch chains only for PRs in no GitHub stack", () => {
    const loose = chain(500, 3);
    const items = [...loose, ...eventDsl()];

    const groups = buildGroups(items);

    expect(groups.map((g) => g.source)).toEqual(["branches", "github"]);
    expect(groups[0]).toMatchObject({ key: `${REPO}#branch/500`, stackNumber: null, size: 3 });
    expect(positions(groups)[0]).toEqual(["500:1/3", "501:2/3", "502:3/3"]);
  });

  it("treats a PR with no stack field as in no stack", () => {
    const items = chain(600, 2).map(({ stack: _stack, ...item }) => item);

    expect(buildGroups(items).map((g) => [g.source, g.entries.length])).toEqual([["branches", 2]]);
  });

  it("does not chain two GitHub stacks by branch", () => {
    const lower = pr(1, { head_ref: "a", stack: { number: 10, size: 1, position: 1 } });
    const upper = pr(2, { base_ref: "a", head_ref: "b", stack: { number: 20, size: 1, position: 1 } });

    expect(numbers(buildGroups([lower, upper]))).toEqual([[1], [2]]);
  });

  it("moves a stack as one unit when sorting and keeps positions when filtering", () => {
    const items = eventDsl();
    items[6] = { ...items[6]!, ci_state: "failure" };
    items[3] = { ...items[3]!, is_draft: true };
    const loose = [pr(1, { ci_state: "pending" }), pr(2, { ci_state: "success" })];

    const byCi = arrangeInbox([...loose, ...items], controls({ sort: "ci" }));
    expect(numbers(byCi).map((g) => g.length)).toEqual([11, 1, 1]);

    const noDrafts = arrangeInbox([...loose, ...items], controls({ hideDrafts: true }));
    const stack = positions(noDrafts).find((g) => g[0]!.startsWith("23897"))!;
    expect(stack).toHaveLength(10);
    expect(stack).not.toContain("23900:4/10");
    expect(stack[3]).toBe("23901:5/10");
    expect(countPrs(noDrafts)).toBe(12);
  });
});

describe("group summary", () => {
  it("rolls CI up to the worst state and ignores PRs with no CI", () => {
    expect(worstCi(["success", null, "pending"])).toBe("pending");
    expect(worstCi(["pending", "failure", "success"])).toBe("failure");
    expect(worstCi(["skipped", "success"])).toBe("success");
    expect(worstCi(["skipped"])).toBe("skipped");
    expect(worstCi([null, null])).toBeNull();
    expect(worstCi([])).toBeNull();
  });

  it("uses the common start of the titles when it is at least two words", () => {
    expect(commonTitle(DSL_TITLES)).toBe("INV-1590: Event DSL");
    expect(commonTitle(["Add foo bar", "Add foo baz"])).toBe("Add foo");
    expect(commonTitle(["Fix a", "Fix b"])).toBeNull();
    expect(commonTitle(["Stack part one", "Stack part one - tests"])).toBe("Stack part one");
    expect(commonTitle([])).toBeNull();
  });

  it("cuts the common title before a bracket or backtick it leaves open", () => {
    const part = (n: number) => `PLAT-3162: Post-\`COMPLETE\` edits (P3) (Part ${n} of 3)`;
    expect(commonTitle([part(1), part(2), part(3)])).toBe("PLAT-3162: Post-`COMPLETE` edits (P3)");
    expect(commonTitle(["CORE-14032: [activity stack 1/9] rates", "CORE-14032: [activity stack 2/9] fees"])).toBeNull();
    expect(commonTitle(["Fix the `foo bar` path", "Fix the `foo baz` path"])).toBe("Fix the");
  });

  it("summarizes the shown PRs of a stack", () => {
    const items = eventDsl();
    items[2] = { ...items[2]!, ci_state: "failure", author: "izaak" };
    items[10] = { ...items[10]!, ci_state: "pending", updated_at: "2026-10-09T00:00:00Z" };
    const [group] = buildGroups(items);

    expect(summarizeGroup(group!)).toMatchObject({
      title: "INV-1590: Event DSL",
      rootTitle: DSL_TITLES[0],
      ci: "failure",
      updatedAt: "2026-10-09T00:00:00Z",
      authors: ["rosgoo", "izaak"],
      additions: 110,
      deletions: 22,
      inStack: 10,
      basedOn: 1,
    });
  });

  it("falls back to the first PR's title", () => {
    const [group] = buildGroups([pr(1, { title: "Carry fix", head_ref: "a" }), pr(2, { title: "Fees", base_ref: "a" })]);

    expect(summarizeGroup(group!).title).toBe("Carry fix");
  });
});

describe("stack expansion", () => {
  const key = `${REPO}#stack/23906`;

  it("starts expanded in your PRs and collapsed in the other lists", () => {
    const none = parseExpansion(null);

    expect(isExpanded(none, "mine", key)).toBe(true);
    expect(isExpanded(none, "direct", key)).toBe(false);
    expect(isExpanded(none, "team", key)).toBe(false);
  });

  it("keeps a choice per list and stack across a save and a load", () => {
    let saved: StackExpansion = withExpansion(parseExpansion(null), "mine", key, false);
    saved = withExpansion(saved, "team", key, true);
    const restored = parseExpansion(serializeExpansion(saved));

    expect(isExpanded(restored, "mine", key)).toBe(false);
    expect(isExpanded(restored, "team", key)).toBe(true);
    expect(isExpanded(restored, "direct", key)).toBe(false);
    expect(isExpanded(restored, "mine", `${REPO}#stack/1`)).toBe(true);
  });

  it("ignores invalid saved data and keeps only the newest choices", () => {
    expect(parseExpansion("not json").size).toBe(0);
    expect(parseExpansion("[true]").size).toBe(0);
    expect([...parseExpansion(JSON.stringify({ a: true, b: "yes" })).keys()]).toEqual(["a"]);

    let saved: StackExpansion = new Map();
    for (let i = 0; i <= MAX_SAVED_EXPANSIONS; i++) saved = withExpansion(saved, "mine", `g${i}`, false);
    saved = withExpansion(saved, "mine", "g1", true);

    expect(saved.size).toBe(MAX_SAVED_EXPANSIONS);
    expect(isExpanded(saved, "mine", "g0")).toBe(true);
    expect(isExpanded(saved, "mine", "g1")).toBe(true);
    expect([...saved.keys()].at(-1)).toBe("mine|g1");
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
