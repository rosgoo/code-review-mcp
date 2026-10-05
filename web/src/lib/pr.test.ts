import { describe, expect, it } from "vitest";
import { recentLabel } from "./inbox";
import {
  attentionChecks,
  countChecks,
  decisionLabel,
  estimatedDiffHeight,
  followViewed,
  headMovedBanner,
  isCollapsed,
  keepHead,
  loadKey,
  setFileViewed,
  statusLetter,
  toggleCollapsed,
  viewerReviewLabel,
} from "./pr";
import type { PrCheck, PrFile, ReviewSummary } from "./types";

describe("recentLabel", () => {
  const base: ReviewSummary = {
    id: "r1",
    kind: "local",
    title: "Code Review",
    repo: null,
    pr_number: null,
    status: "open",
    mode: "diff",
    url: "",
    created_at: "",
    updated_at: "",
  };

  it("leads a PR review with repo#number", () => {
    expect(recentLabel({ ...base, kind: "pr", repo: "o/r", pr_number: 7, title: "T" })).toEqual({
      primary: "o/r#7",
      secondary: "T",
    });
  });

  it("shows a local review by its title", () => {
    expect(recentLabel(base)).toEqual({ primary: "Code Review", secondary: null });
  });
});

describe("viewed collapse state", () => {
  const file = (viewed: boolean): PrFile => ({
    path: "a.py",
    old_path: null,
    status: "modified",
    additions: 1,
    deletions: 1,
    binary: false,
    viewed,
  });

  it("collapses viewed files and expands the rest by default", () => {
    expect(isCollapsed(file(true), new Map())).toBe(true);
    expect(isCollapsed(file(false), new Map())).toBe(false);
  });

  it("lets the user expand a viewed file and collapse an unviewed one", () => {
    expect(isCollapsed(file(true), toggleCollapsed(new Map(), file(true)))).toBe(false);
    expect(isCollapsed(file(false), toggleCollapsed(new Map(), file(false)))).toBe(true);
  });

  it("follows the viewed state again after it changes", () => {
    const expanded = toggleCollapsed(new Map(), file(false));
    expect(isCollapsed(file(true), followViewed(expanded, "a.py"))).toBe(true);
    expect(isCollapsed(file(false), followViewed(expanded, "a.py"))).toBe(false);
  });

  it("updates one file's viewed flag", () => {
    const files = [file(false), { ...file(false), path: "b.py" }];
    expect(setFileViewed(files, "b.py", true).map((f) => f.viewed)).toEqual([false, true]);
  });
});

describe("headMovedBanner", () => {
  const event = { old_head_sha: "aaaaaaa1111", new_head_sha: "bbbbbbb2222" };

  it("shows the shown head and the newest head, shortened", () => {
    expect(headMovedBanner("aaaaaaa1111", event)).toEqual({ from: "aaaaaaa", to: "bbbbbbb" });
  });

  it("starts from the shown head after several moves", () => {
    const later = { old_head_sha: "bbbbbbb2222", new_head_sha: "ccccccc3333" };
    expect(headMovedBanner("aaaaaaa1111", later)).toEqual({ from: "aaaaaaa", to: "ccccccc" });
  });

  it("hides once the page shows the new head, or with no event", () => {
    expect(headMovedBanner("bbbbbbb2222", event)).toBeNull();
    expect(headMovedBanner("aaaaaaa1111", null)).toBeNull();
    expect(headMovedBanner(null, event)).toBeNull();
  });
});

describe("estimatedDiffHeight", () => {
  it("grows with changed lines between a floor and a cap", () => {
    expect(estimatedDiffHeight({ additions: 0, deletions: 0, binary: false })).toBe(48);
    expect(estimatedDiffHeight({ additions: 10, deletions: 5, binary: false })).toBe(300);
    expect(estimatedDiffHeight({ additions: 5000, deletions: 0, binary: false })).toBe(4000);
    expect(estimatedDiffHeight({ additions: null, deletions: null, binary: true })).toBe(48);
  });
});

describe("keepHead", () => {
  const entries = new Map([
    [loadKey("aaa", "x.py"), 1],
    [loadKey("aaa", "y.py"), 2],
    [loadKey("bbb", "x.py"), 3],
  ]);

  it("drops file contents loaded for another head", () => {
    expect([...keepHead(entries, "bbb").keys()]).toEqual(["bbb:x.py"]);
  });

  it("drops everything for a closed review", () => {
    expect(keepHead(entries, null).size).toBe(0);
  });

  it("returns the same map when nothing is dropped", () => {
    const current = new Map([[loadKey("aaa", "x.py"), 1]]);
    expect(keepHead(current, "aaa")).toBe(current);
  });
});

describe("checks", () => {
  const check = (name: string, state: PrCheck["state"]): PrCheck => ({
    name,
    state,
    url: null,
    workflow: null,
  });
  const checks = [
    check("lint", "success"),
    check("tests", "failure"),
    check("build", "pending"),
    check("docs", "skipped"),
    check("e2e", "failure"),
  ];

  it("counts checks by state", () => {
    expect(countChecks(checks)).toEqual({ failure: 2, pending: 1, success: 1, skipped: 1 });
  });

  it("lists failing then pending checks", () => {
    expect(attentionChecks(checks).map((c) => c.name)).toEqual(["e2e", "tests", "build"]);
  });

  it("labels review decisions and file statuses", () => {
    expect(decisionLabel("CHANGES_REQUESTED")).toBe("Changes requested");
    expect(decisionLabel(null)).toBeNull();
    expect(statusLetter("renamed")).toBe("R");
    expect(viewerReviewLabel("CHANGES_REQUESTED")).toBe("You requested changes");
    expect(viewerReviewLabel(null)).toBeNull();
  });
});
