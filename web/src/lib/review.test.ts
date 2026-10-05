import { describe, expect, it } from "vitest";
import {
  OUTSIDE_DIFF_HINT,
  applyThreadEvent,
  clampSelection,
  confirmContent,
  countThreads,
  eventButtons,
  isCommentable,
  pendingThreads,
  placeThreads,
  previewBody,
  reviewAnnotations,
  staleBlockLabel,
  staleIdsFrom,
  threadEventNeedsRefetch,
  type SubmitState,
} from "./review";
import type { Commentable, ReviewThread } from "./types";

const COMMENTABLE: Commentable = {
  additions: [
    [10, 20],
    [40, 45],
  ],
  deletions: [[8, 15]],
};

function thread(id: string, overrides: Partial<ReviewThread> = {}): ReviewThread {
  return {
    id,
    kind: "review_comment",
    status: "draft",
    path: "app.py",
    side: "additions",
    line: 12,
    start_line: null,
    start_side: null,
    anchor_sha: "head1",
    created_by: "user",
    created_at: "2026-10-05T00:00:00Z",
    updated_at: "2026-10-05T00:00:00Z",
    github_url: null,
    messages: [{ id: `m-${id}`, author: "user", body: `body ${id}`, created_at: "" }],
    ...overrides,
  };
}

describe("commentable lines", () => {
  it("checks each side against its own hunk ranges", () => {
    expect(isCommentable(COMMENTABLE, "additions", 10)).toBe(true);
    expect(isCommentable(COMMENTABLE, "additions", 21)).toBe(false);
    expect(isCommentable(COMMENTABLE, "deletions", 15)).toBe(true);
    expect(isCommentable(COMMENTABLE, "deletions", 16)).toBe(false);
    expect(isCommentable(undefined, "additions", 10)).toBe(false);
  });

  it("anchors a single commentable line", () => {
    expect(clampSelection("a.py", { start: 12, end: 12, side: "additions" }, COMMENTABLE)).toEqual({
      ok: true,
      clamped: false,
      anchor: { path: "a.py", side: "additions", line: 12 },
    });
  });

  it("keeps a range inside one hunk and orders it", () => {
    expect(clampSelection("a.py", { start: 18, end: 11, side: "additions" }, COMMENTABLE)).toEqual({
      ok: true,
      clamped: false,
      anchor: {
        path: "a.py",
        side: "additions",
        line: 18,
        start_line: 11,
        start_side: "additions",
      },
    });
  });

  it("clamps a range to the hunk of its last line, or else its first line", () => {
    expect(
      clampSelection("a.py", { start: 5, end: 14, side: "additions" }, COMMENTABLE),
    ).toMatchObject({
      ok: true,
      clamped: true,
      anchor: { line: 14, start_line: 10 },
    });
    expect(
      clampSelection("a.py", { start: 18, end: 30, side: "additions" }, COMMENTABLE),
    ).toMatchObject({
      ok: true,
      clamped: true,
      anchor: { line: 20, start_line: 18 },
    });
    expect(
      clampSelection("a.py", { start: 15, end: 42, side: "additions" }, COMMENTABLE),
    ).toMatchObject({
      ok: true,
      clamped: true,
      anchor: { line: 42, start_line: 40 },
    });
    expect(
      clampSelection("a.py", { start: 19, end: 25, side: "additions" }, COMMENTABLE),
    ).toMatchObject({
      anchor: { line: 20, start_line: 19 },
    });
  });

  it("rejects a selection with no commentable line, or with no ranges loaded", () => {
    expect(clampSelection("a.py", { start: 25, end: 30, side: "additions" }, COMMENTABLE)).toEqual({
      ok: false,
      hint: OUTSIDE_DIFF_HINT,
    });
    expect(clampSelection("a.py", { start: 12, end: 12 }, undefined)).toMatchObject({ ok: false });
  });

  it("accepts a cross-side range only when both ends are commentable", () => {
    expect(
      clampSelection(
        "a.py",
        { start: 9, side: "deletions", end: 11, endSide: "additions" },
        COMMENTABLE,
      ),
    ).toEqual({
      ok: true,
      clamped: false,
      anchor: { path: "a.py", side: "additions", line: 11, start_line: 9, start_side: "deletions" },
    });
    expect(
      clampSelection(
        "a.py",
        { start: 3, side: "deletions", end: 11, endSide: "additions" },
        COMMENTABLE,
      ),
    ).toMatchObject({ ok: false });
  });
});

describe("thread placement", () => {
  const threads = [
    thread("draft"),
    thread("posted", { status: "posted", github_url: "https://github.com/x" }),
    thread("old-posted", { status: "posted", anchor_sha: "head0" }),
    thread("stale", { status: "stale", anchor_sha: "head0" }),
    thread("file", { line: 0 }),
    thread("other", { path: "b.py", side: "deletions", line: 9 }),
    thread("question", { kind: "question" }),
  ];

  it("puts current drafts and posted comments inline and the rest in the file block", () => {
    const placed = placeThreads(threads, "head1");
    expect(placed.get("app.py")?.inline.map((t) => t.id)).toEqual(["draft", "posted"]);
    expect(placed.get("app.py")?.block.map((t) => t.id)).toEqual(["old-posted", "stale", "file"]);
    expect(placed.get("b.py")?.inline.map((t) => t.id)).toEqual(["other"]);
  });

  it("groups inline threads and the composer by side and line", () => {
    const inline = placeThreads(threads, "head1").get("app.py")!.inline;
    expect(reviewAnnotations(inline, { path: "app.py", side: "deletions", line: 9 })).toEqual([
      {
        side: "additions",
        lineNumber: 12,
        metadata: { threadIds: ["draft", "posted"], composer: false },
      },
      { side: "deletions", lineNumber: 9, metadata: { threadIds: [], composer: true } },
    ]);
    expect(reviewAnnotations([], { path: "app.py", side: "additions", line: 0 })).toEqual([]);
  });

  it("counts drafts and stale comments and lists them in file order", () => {
    expect(countThreads(threads)).toEqual({ drafts: 3, stale: 1 });
    expect(pendingThreads(threads, ["b.py", "app.py"]).map((t) => t.id)).toEqual([
      "other",
      "file",
      "draft",
      "stale",
    ]);
  });
});

describe("submit buttons", () => {
  const base: SubmitState = {
    allowedEvents: ["COMMENT", "APPROVE", "REQUEST_CHANGES"],
    isAuthor: false,
    drafts: 1,
    stale: 0,
    summary: "",
    busy: false,
  };
  const reasons = (state: Partial<SubmitState>) =>
    Object.fromEntries(eventButtons({ ...base, ...state }).map((b) => [b.event, b.reason]));

  it("enables what GitHub allows", () => {
    expect(reasons({ summary: "LGTM" })).toEqual({
      COMMENT: null,
      APPROVE: null,
      REQUEST_CHANGES: null,
    });
  });

  it("explains why events are blocked on the user's own PR", () => {
    expect(reasons({ allowedEvents: ["COMMENT"], isAuthor: true, summary: "x" })).toEqual({
      COMMENT: null,
      APPROVE: "You can't approve your own PR",
      REQUEST_CHANGES: "You can't request changes on your own PR",
    });
  });

  it("blocks submit while stale comments exist", () => {
    expect(reasons({ stale: 2, summary: "x" }).APPROVE).toBe(
      "Re-anchor or delete the stale comments first",
    );
  });

  it("needs content for a comment and a summary to request changes", () => {
    expect(reasons({ drafts: 0 })).toEqual({
      COMMENT: "Write a summary or a comment first",
      APPROVE: null,
      REQUEST_CHANGES: "Write a summary to request changes",
    });
    expect(eventButtons({ ...base, busy: true }).every((b) => b.disabled)).toBe(true);
  });

  it("treats missing allowed events as all allowed", () => {
    expect(reasons({ allowedEvents: undefined, summary: "x" }).APPROVE).toBeNull();
  });
});

describe("confirm content", () => {
  it("lists the event, the summary, and every comment with a short preview", () => {
    const drafts = [
      thread("a", { start_line: 10, line: 12 }),
      thread("b", { line: 0, path: "README.md" }),
      thread("c", {
        line: 3,
        messages: [{ id: "m", author: "user", body: "first\nsecond\nthird", created_at: "" }],
      }),
    ];

    expect(confirmContent("REQUEST_CHANGES", "  Please fix.  ", drafts)).toEqual({
      event: "REQUEST_CHANGES",
      eventLabel: "Request changes",
      summary: "Please fix.",
      comments: [
        { id: "a", location: "app.py:10–12", preview: "body a" },
        { id: "b", location: "README.md (file)", preview: "body b" },
        { id: "c", location: "app.py:3", preview: "first\nsecond…" },
      ],
    });
  });

  it("cuts a long preview", () => {
    expect(previewBody("x".repeat(400))).toHaveLength(161);
  });
});

describe("thread events", () => {
  const threads = [thread("a"), thread("b")];

  it("adds, replaces, and deletes threads", () => {
    const added = applyThreadEvent(threads, { type: "thread_added", thread: thread("c") });
    expect(added?.map((t) => t.id)).toEqual(["a", "b", "c"]);
    const updated = applyThreadEvent(threads, {
      type: "thread_updated",
      thread: thread("a", { status: "posted" }),
    });
    expect(updated?.map((t) => t.status)).toEqual(["posted", "draft"]);
    expect(
      applyThreadEvent(threads, { type: "thread_deleted", thread_id: "a" })?.map((t) => t.id),
    ).toEqual(["b"]);
    expect(
      applyThreadEvent(threads, { type: "thread_deleted", comment_id: "b" })?.map((t) => t.id),
    ).toEqual(["a"]);
  });

  it("marks threads stale", () => {
    expect(
      applyThreadEvent(threads, { type: "threads_stale", thread_ids: ["b"] })?.map((t) => t.status),
    ).toEqual(["draft", "stale"]);
  });

  it("asks for a refetch when an event lacks the thread, and ignores other events", () => {
    expect(applyThreadEvent(threads, { type: "thread_added" })).toBeNull();
    expect(applyThreadEvent(threads, { type: "thread_deleted" })).toBeNull();
    expect(applyThreadEvent(threads, { type: "review_submitted" })).toBeNull();
    expect(applyThreadEvent(threads, { type: "view_updated" })).toBe(threads);
    expect(threadEventNeedsRefetch({ type: "thread_added" })).toBe(true);
    expect(threadEventNeedsRefetch({ type: "thread_added", thread: thread("x") })).toBe(false);
    expect(threadEventNeedsRefetch({ type: "review_submitted" })).toBe(true);
    expect(threadEventNeedsRefetch({ type: "threads_stale", thread_ids: [] })).toBe(false);
  });

  it("labels the stale block with the right verb", () => {
    expect(staleBlockLabel(1)).toBe("1 stale comment blocks submit");
    expect(staleBlockLabel(2)).toBe("2 stale comments block submit");
  });

  it("reads stale ids from a 409 body", () => {
    expect(staleIdsFrom({ error: "stale", stale_thread_ids: ["a", 3, "b"] })).toEqual(["a", "b"]);
    expect(staleIdsFrom({ error: "x" })).toEqual([]);
    expect(staleIdsFrom(null)).toEqual([]);
  });
});
