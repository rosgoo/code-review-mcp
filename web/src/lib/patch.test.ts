import { parseDiffFromFile } from "@pierre/diffs";
import { describe, expect, it } from "vitest";
import { anchorFromDiffSelection, anchorFromFileSelection, toNewComment } from "./anchors";
import { fileEntries } from "./entries";
import { changeCounts, describeLine, splitPatch } from "./patch";

const PATCH = `diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,4 +1,4 @@
 import os
-x = 1
+x = 2
 print(x)
 done()
@@ -10,3 +10,4 @@ def later():
 a = 1
+b = 2
 c = 3
 d = 4
diff --git a/README.md b/README.md
new file mode 100644
--- /dev/null
+++ b/README.md
@@ -0,0 +1,2 @@
+# Title
+text
diff --git a/old.txt b/old.txt
deleted file mode 100644
--- a/old.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-gone
-too
`;

describe("splitPatch", () => {
  it("returns one entry per file in patch order", () => {
    const files = splitPatch(PATCH);

    expect(files.map((f) => [f.path, f.fileDiff.type])).toEqual([
      ["src/app.py", "change"],
      ["README.md", "new"],
      ["old.txt", "deleted"],
    ]);
    expect(files.map((f) => changeCounts(f.fileDiff))).toEqual([
      { additions: 2, deletions: 1 },
      { additions: 2, deletions: 0 },
      { additions: 0, deletions: 2 },
    ]);
  });

  it("returns nothing for an empty patch", () => {
    expect(splitPatch("")).toEqual([]);
    expect(splitPatch("\n  \n")).toEqual([]);
  });
});

describe("describeLine", () => {
  const [app, readme, removed] = splitPatch(PATCH).map((f) => f.fileDiff);

  it("finds changed and context lines in a patch", () => {
    expect(describeLine(app!, "deletions", 2)).toEqual({
      text: "x = 1",
      changed: true,
      additionsLine: null,
    });
    expect(describeLine(app!, "additions", 2)).toEqual({
      text: "x = 2",
      changed: true,
      additionsLine: 2,
    });
    expect(describeLine(app!, "deletions", 12)).toEqual({
      text: "d = 4",
      changed: false,
      additionsLine: 13,
    });
    expect(describeLine(app!, "additions", 11)).toEqual({
      text: "b = 2",
      changed: true,
      additionsLine: 11,
    });
  });

  it("maps unchanged lines between hunks by the running offset", () => {
    expect(describeLine(app!, "deletions", 20)).toEqual({
      text: "",
      changed: false,
      additionsLine: 21,
    });
  });

  it("handles new and deleted files", () => {
    expect(describeLine(readme!, "additions", 1)?.text).toBe("# Title");
    expect(describeLine(readme!, "deletions", 1)).toBeNull();
    expect(describeLine(removed!, "deletions", 2)?.text).toBe("too");
    expect(describeLine(removed!, "additions", 1)).toBeNull();
  });

  it("reads unchanged text outside hunks from full file contents", () => {
    const old = Array.from({ length: 30 }, (_, i) => `line ${i + 1}`).join("\n") + "\n";
    const next = old.replace("line 3\n", "line 3\ninserted\n");
    const full = parseDiffFromFile(
      { name: "f.txt", contents: old },
      { name: "f.txt", contents: next },
    );

    expect(describeLine(full, "deletions", 25)).toEqual({
      text: "line 25",
      changed: false,
      additionsLine: 26,
    });
    expect(describeLine(full, "additions", 4)?.changed).toBe(true);
  });
});

describe("anchors", () => {
  const app = splitPatch(PATCH)[0]!.fileDiff;

  it("moves a context line selected on the deletions side to the additions side", () => {
    expect(
      anchorFromDiffSelection("src/app.py", app, { start: 3, end: 3, side: "deletions" }),
    ).toEqual({
      path: "src/app.py",
      side: "additions",
      line: 3,
      lineContent: "print(x)",
    });
  });

  it("keeps a deleted line on the deletions side", () => {
    expect(
      anchorFromDiffSelection("src/app.py", app, { start: 2, end: 2, side: "deletions" }),
    ).toEqual({
      path: "src/app.py",
      side: "deletions",
      line: 2,
      lineContent: "x = 1",
    });
  });

  it("orders a range selected upwards and joins its lines", () => {
    const anchor = anchorFromDiffSelection("src/app.py", app, {
      start: 4,
      end: 2,
      side: "additions",
    });

    expect(anchor).toEqual({
      path: "src/app.py",
      side: "additions",
      line: 4,
      startLine: 2,
      startSide: "additions",
      lineContent: "x = 2\nprint(x)\ndone()",
    });
    expect(toNewComment(anchor!, "hm")).toEqual({
      path: "src/app.py",
      side: "additions",
      line: 4,
      start_line: 2,
      start_side: "additions",
      line_content: "x = 2\nprint(x)\ndone()",
      body: "hm",
    });
  });

  it("keeps both sides of a range that crosses from deletions to additions", () => {
    expect(
      anchorFromDiffSelection("src/app.py", app, {
        start: 2,
        side: "deletions",
        end: 2,
        endSide: "additions",
      }),
    ).toEqual({
      path: "src/app.py",
      side: "additions",
      line: 2,
      startLine: 2,
      startSide: "deletions",
      lineContent: "x = 2",
    });
  });

  it("builds a plain file anchor from a selection", () => {
    const content = "a\nb\nc\nd\n";

    expect(anchorFromFileSelection("f.md", content, { start: 2, end: 2 })).toEqual({
      path: "f.md",
      side: "additions",
      line: 2,
      lineContent: "b",
    });
    expect(
      toNewComment(anchorFromFileSelection("f.md", content, { start: 3, end: 1 }), "x"),
    ).toEqual({
      path: "f.md",
      side: "additions",
      line: 3,
      start_line: 1,
      start_side: "additions",
      line_content: "a\nb\nc",
      body: "x",
    });
  });
});

describe("fileEntries", () => {
  const base = { review_id: "r", kind: "local" as const, title: "t" };
  const appFile = { path: "src/app.py", content: "new", language: "python" };

  it("renders a patch-only review from the patch", () => {
    const entries = fileEntries({ ...base, mode: "diff", diff: PATCH });
    expect(entries.map((e) => [e.kind, e.path])).toEqual([
      ["patch", "src/app.py"],
      ["patch", "README.md"],
      ["patch", "old.txt"],
    ]);
  });

  it("uses full contents when old_content exists and the patch otherwise", () => {
    const entries = fileEntries({
      ...base,
      mode: "files",
      diff: PATCH,
      files: [
        { ...appFile, old_content: "old" },
        { path: "README.md", content: "# Title\ntext\n", language: "markdown", old_content: null },
        { path: "extra.txt", content: "x", language: "plaintext" },
      ],
    });

    expect(entries.map((e) => [e.kind, e.path])).toEqual([
      ["full", "src/app.py"],
      ["patch", "README.md"],
      ["patch", "old.txt"],
      ["plain", "extra.txt"],
    ]);
    expect(entries[0]).toMatchObject({
      oldFile: { name: "src/app.py", contents: "old" },
      newFile: { name: "src/app.py", contents: "new" },
    });
  });

  it("renders show_files reviews as plain files", () => {
    const entries = fileEntries({ ...base, mode: "files", files: [appFile] });
    expect(entries).toEqual([{ kind: "plain", path: "src/app.py", file: appFile }]);
  });
});
