import { describe, expect, it } from "vitest";
import {
  citationHref,
  findCitations,
  parseCitationHref,
  splitCitations,
  wholeCitation,
} from "./citations";

const cites = (text: string) => findCitations(text).map((c) => `${c.path}:${c.start}-${c.end}`);

describe("findCitations", () => {
  it("finds a path with a line or a range", () => {
    expect(cites("See src/code_review_mcp/agents.py:230 and web/src/lib/agent.ts:12-40.")).toEqual([
      "src/code_review_mcp/agents.py:230-230",
      "web/src/lib/agent.ts:12-40",
    ]);
  });

  it("handles dashes, dots, dot files, en dashes, and a leading ./", () => {
    expect(cites("my-file.test.ts:3, .github/workflows/ci.yml:7–9, ./README.md:1")).toEqual([
      "my-file.test.ts:3-3",
      ".github/workflows/ci.yml:7-9",
      "README.md:1-1",
    ]);
  });

  it("orders a reversed range and keeps the cited text", () => {
    const [match] = findCitations("look at a/b.py:20-12 here");
    expect(match).toMatchObject({ path: "a/b.py", start: 12, end: 20, text: "a/b.py:20-12" });
  });

  it("ignores times, ratios, ports, and URLs", () => {
    expect(cites("The job ran at 10:30 and 12:45:07, a 16:9 ratio.")).toEqual([]);
    expect(cites("Open http://127.0.0.1:7790/r/abc or localhost:8080.")).toEqual([]);
    expect(cites("https://github.com/Maybern/maybern/blob/main/src/a.py:12")).toEqual([]);
    expect(cites("version 1.2.3:4 and ../up.py:3")).toEqual([]);
    expect(cites("foo.py:12abc")).toEqual([]);
  });

  it("ends a match before a column number or a path that continues", () => {
    expect(cites("a.py:12:5")).toEqual(["a.py:12-12"]);
    expect(cites("src/a.py:12/more")).toEqual([]);
  });
});

describe("splitCitations and wholeCitation", () => {
  it("splits text around the citations", () => {
    expect(splitCitations("x a.py:1 y").map((s) => (s.kind === "cite" ? `[${s.text}]` : s.text))).toEqual([
      "x ",
      "[a.py:1]",
      " y",
    ]);
    expect(splitCitations("no cites")).toEqual([{ kind: "text", text: "no cites" }]);
  });

  it("accepts inline code that is one citation only", () => {
    expect(wholeCitation(" src/a.py:3-4 ")).toMatchObject({ path: "src/a.py", start: 3, end: 4 });
    expect(wholeCitation("src/a.py:3 and more")).toBeNull();
    expect(wholeCitation("print(x)")).toBeNull();
  });

  it("round-trips a citation through its link href", () => {
    const href = citationHref({ path: "dir/a b:c.py", start: 2, end: 5 }, true);
    expect(parseCitationHref(href)).toEqual({ path: "dir/a b:c.py", start: 2, end: 5, code: true });
    expect(parseCitationHref("https://example.com")).toBeNull();
    expect(parseCitationHref(undefined)).toBeNull();
  });
});
