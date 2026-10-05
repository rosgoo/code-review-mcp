import { describe, expect, it } from "vitest";
import { diffAnnotations, fileAnnotations } from "./annotations";
import { applyCommentEvent, lineLabel } from "./comments";
import type { Comment } from "./types";

function comment(id: string, overrides: Partial<Comment> = {}): Comment {
  return {
    id,
    file_path: "app.py",
    line_number: 2,
    line_type: "add",
    line_content: "",
    user_message: `message ${id}`,
    timestamp: "2026-10-05T00:00:00Z",
    status: "draft",
    replies: [],
    ...overrides,
  };
}

describe("diffAnnotations", () => {
  it("groups threads by side and line and skips other files", () => {
    const comments = [
      comment("a"),
      comment("b", { line_type: "context" }),
      comment("c", { line_type: "delete" }),
      comment("d", { file_path: "other.py" }),
      comment("e", { line_number: 7 }),
    ];

    expect(diffAnnotations(comments, "app.py", null)).toEqual([
      { side: "additions", lineNumber: 2, metadata: { threadIds: ["a", "b"], composer: false } },
      { side: "deletions", lineNumber: 2, metadata: { threadIds: ["c"], composer: false } },
      { side: "additions", lineNumber: 7, metadata: { threadIds: ["e"], composer: false } },
    ]);
  });

  it("adds the composer to its line, alone or next to threads", () => {
    const anchor = { path: "app.py", side: "deletions" as const, line: 2, lineContent: "" };
    const elsewhere = { ...anchor, side: "additions" as const, line: 9 };

    expect(diffAnnotations([comment("c", { line_type: "delete" })], "app.py", anchor)).toEqual([
      { side: "deletions", lineNumber: 2, metadata: { threadIds: ["c"], composer: true } },
    ]);
    expect(diffAnnotations([], "app.py", elsewhere)).toEqual([
      { side: "additions", lineNumber: 9, metadata: { threadIds: [], composer: true } },
    ]);
    expect(diffAnnotations([], "other.py", elsewhere)).toEqual([]);
  });
});

describe("fileAnnotations", () => {
  it("maps threads to line annotations with no side", () => {
    const anchor = { path: "app.py", side: "additions" as const, line: 2, lineContent: "" };

    expect(
      fileAnnotations([comment("a"), comment("b", { line_number: 4 })], "app.py", anchor),
    ).toEqual([
      { lineNumber: 2, metadata: { threadIds: ["a"], composer: true } },
      { lineNumber: 4, metadata: { threadIds: ["b"], composer: false } },
    ]);
  });
});

describe("applyCommentEvent", () => {
  const reply = {
    id: "r1",
    comment_id: "a",
    author: "claude" as const,
    message: "done",
    timestamp: "2026-10-05T00:00:01Z",
  };

  it("adds, deletes, and resolves threads once", () => {
    const start = [comment("a")];
    const added = applyCommentEvent(start, { type: "comment_added", comment: comment("b") });

    expect(added.map((c) => c.id)).toEqual(["a", "b"]);
    expect(applyCommentEvent(added, { type: "comment_added", comment: comment("b") })).toBe(added);
    expect(
      applyCommentEvent(added, { type: "thread_deleted", comment_id: "a" }).map((c) => c.id),
    ).toEqual(["b"]);
    expect(applyCommentEvent(start, { type: "thread_deleted", comment_id: "zz" })).toBe(start);
    expect(applyCommentEvent(start, { type: "comment_resolved", comment_id: "a" })[0]?.status).toBe(
      "resolved",
    );
  });

  it("appends a reply once and reopens when asked", () => {
    const resolved = [comment("a", { status: "resolved" })];
    const once = applyCommentEvent(resolved, {
      type: "reply_added",
      comment_id: "a",
      reply,
      reopened: true,
    });
    const twice = applyCommentEvent(once, {
      type: "reply_added",
      comment_id: "a",
      reply,
      reopened: false,
    });

    expect(once[0]?.status).toBe("submitted");
    expect(twice[0]?.replies).toEqual([reply]);
    expect(twice[0]?.status).toBe("submitted");
  });

  it("ignores events that do not change threads", () => {
    const start = [comment("a")];
    expect(applyCommentEvent(start, { type: "view_updated" })).toBe(start);
  });
});

describe("lineLabel", () => {
  it("labels single lines, ranges, and overall feedback", () => {
    expect(lineLabel(comment("a"))).toBe("L2");
    expect(lineLabel(comment("a", { start_line: 1, line_number: 3 }))).toBe("L1–L3");
    expect(lineLabel(comment("a", { file_path: "(overall)", line_number: 0 }))).toBe("Overall");
  });
});
