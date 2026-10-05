import { describe, expect, it } from "vitest";
import { resolveRoute, reviewPath } from "./router";
import { buildFileTree, filterPaths } from "./tree";

describe("resolveRoute", () => {
  it("shows the inbox at the root", () => {
    expect(resolveRoute("/", "")).toEqual({ kind: "inbox" });
    expect(resolveRoute("/", "?other=1")).toEqual({ kind: "inbox" });
  });

  it("redirects the legacy ?review= link to /r/<id>", () => {
    expect(resolveRoute("/", "?review=abc123")).toEqual({ kind: "redirect", to: "/r/abc123" });
    expect(resolveRoute("/", "?review=a%2Fb")).toEqual({ kind: "redirect", to: "/r/a%2Fb" });
  });

  it("opens a review by id", () => {
    expect(resolveRoute("/r/abc123", "")).toEqual({ kind: "review", id: "abc123" });
    expect(resolveRoute("/r/abc123/", "")).toEqual({ kind: "review", id: "abc123" });
    expect(resolveRoute(reviewPath("a/b"), "")).toEqual({ kind: "review", id: "a/b" });
  });

  it("does not match other paths", () => {
    expect(resolveRoute("/r/", "")).toEqual({ kind: "not_found" });
    expect(resolveRoute("/r/a/b", "")).toEqual({ kind: "not_found" });
    expect(resolveRoute("/static/index.html", "")).toEqual({ kind: "not_found" });
  });
});

describe("buildFileTree", () => {
  it("nests files, merges single-child directories, and sorts directories first", () => {
    expect(
      buildFileTree(["src/pkg/b.py", "src/pkg/a.py", "README.md", "web/src/lib/x.ts"]),
    ).toEqual([
      {
        kind: "dir",
        name: "src/pkg",
        children: [
          { kind: "file", name: "a.py", path: "src/pkg/a.py" },
          { kind: "file", name: "b.py", path: "src/pkg/b.py" },
        ],
      },
      {
        kind: "dir",
        name: "web/src/lib",
        children: [{ kind: "file", name: "x.ts", path: "web/src/lib/x.ts" }],
      },
      { kind: "file", name: "README.md", path: "README.md" },
    ]);
  });

  it("filters paths case-insensitively", () => {
    expect(filterPaths(["src/App.tsx", "README.md"], "app")).toEqual(["src/App.tsx"]);
    expect(filterPaths(["a", "b"], "  ")).toEqual(["a", "b"]);
  });
});
