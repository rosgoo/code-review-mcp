import { describe, expect, it } from "vitest";
import {
  EMPTY_LIVE,
  applyAgentEvent,
  applyAgentMessage,
  composerAnchor,
  composerFor,
  formatCost,
  formatTokens,
  markSent,
  questionState,
  questionStateLabel,
  questionThreads,
  queueLabel,
  splitByHunks,
  type AgentLive,
} from "./agent";
import type { AgentStatus, Commentable, ReviewEvent, ReviewThread } from "./types";

function status(overrides: Partial<AgentStatus> = {}): AgentStatus {
  return {
    state: "idle",
    session_id: "s1",
    model: "claude-opus-5-5",
    cost_usd: 0,
    context_tokens: null,
    queue: [],
    running_thread_id: null,
    warmup: { status: "none", thread_id: null },
    ...overrides,
  };
}

function question(id: string, overrides: Partial<ReviewThread> = {}): ReviewThread {
  return {
    id,
    kind: "question",
    status: "submitted",
    path: "src/a.py",
    side: "additions",
    line: 10,
    start_line: null,
    start_side: null,
    anchor_sha: "head",
    created_by: "user",
    created_at: "2026-10-05T12:00:00Z",
    updated_at: "2026-10-05T12:00:00Z",
    github_url: null,
    messages: [
      { id: `${id}-m1`, author: "user", body: "Why?", created_at: "2026-10-05T12:00:00Z" },
    ],
    ...overrides,
  };
}

const run = (live: AgentLive, events: readonly ReviewEvent[]) =>
  events.reduce(applyAgentEvent, live);
const statusEvent = (overrides: Partial<AgentStatus>): ReviewEvent => ({
  type: "agent_status",
  ...status(overrides),
});

describe("agent SSE reducer", () => {
  it("streams deltas, then the message replaces the stream", () => {
    const thread = question("q1");
    let live = run(EMPTY_LIVE, [
      statusEvent({ state: "running", running_thread_id: "q1" }),
      { type: "agent_delta", thread_id: "q1", text: "It " },
      { type: "agent_delta", thread_id: "q1", text: "returns early." },
    ]);
    expect(live.streams.q1).toBe("It returns early.");
    expect(questionState(thread, live)).toEqual({ kind: "running" });

    const message = {
      id: "a1",
      author: "agent" as const,
      body: "It returns early.",
      created_at: "",
    };
    live = run(live, [
      { type: "agent_message", thread_id: "q1", message },
      statusEvent({ state: "idle" }),
    ]);
    const threads = applyAgentMessage([thread], "q1", message)!;
    expect(live.streams.q1).toBeUndefined();
    expect(threads[0]!.messages.map((m) => m.author)).toEqual(["user", "agent"]);
    expect(questionState(threads[0]!, live)).toEqual({ kind: "answered" });
  });

  it("keeps an error until the thread gets a new turn", () => {
    const thread = question("q1");
    let live = run(EMPTY_LIVE, [
      { type: "agent_delta", thread_id: "q1", text: "partial" },
      {
        type: "agent_error",
        thread_id: "q1",
        error: "The agent turn ended with error_during_execution",
      },
      statusEvent({ state: "error" }),
    ]);
    expect(live.streams.q1).toBeUndefined();
    expect(questionState(thread, live)).toEqual({
      kind: "error",
      error: "The agent turn ended with error_during_execution",
    });

    live = markSent(live, "q1");
    expect(questionState(thread, live)).toEqual({ kind: "sent" });
    live = run(live, [statusEvent({ state: "queued", queue: ["q1"] })]);
    expect(live.errors.q1).toBeUndefined();
    expect(live.sent.q1).toBeUndefined();
    expect(questionState(thread, live)).toEqual({ kind: "queued", ahead: 0 });
  });

  it("ends a stopped turn with the message the backend marks stopped", () => {
    const thread = question("q1");
    let live = run(EMPTY_LIVE, [
      statusEvent({ state: "running", running_thread_id: "q1" }),
      { type: "agent_delta", thread_id: "q1", text: "Half an" },
    ]);
    const stopped = {
      id: "a1",
      author: "agent" as const,
      body: "Half an\n\n_(stopped)_",
      created_at: "",
    };
    live = run(live, [
      { type: "agent_message", thread_id: "q1", message: stopped },
      statusEvent({ state: "idle" }),
    ]);
    const [after] = applyAgentMessage([thread], "q1", stopped)!;
    expect(live.streams).toEqual({});
    expect(after!.messages.at(-1)!.body).toContain("_(stopped)_");
    expect(questionState(after!, live)).toEqual({ kind: "answered" });
  });

  it("fills the overview's first message in place", () => {
    const overview = question("ov", {
      path: "",
      line: 0,
      created_by: "agent",
      messages: [{ id: "ov-m1", author: "agent", body: "", created_at: "" }],
    });
    const filled = { id: "ov-m1", author: "agent" as const, body: "## Overview", created_at: "" };

    const [after] = applyAgentMessage([overview], "ov", filled)!;

    expect(after!.messages).toEqual([filled]);
    expect(applyAgentMessage([overview], "other", filled)).toBeNull();
  });

  it("ignores events that are not about the agent", () => {
    expect(applyAgentEvent(EMPTY_LIVE, { type: "view_updated" })).toBe(EMPTY_LIVE);
  });
});

describe("queue and state labels", () => {
  it("counts the running turn and the queue ahead", () => {
    const live = run(EMPTY_LIVE, [
      statusEvent({ state: "running", running_thread_id: "q0", queue: ["q1", "q2"] }),
    ]);

    expect(questionState(question("q1"), live)).toEqual({ kind: "queued", ahead: 1 });
    expect(questionState(question("q2"), live)).toEqual({ kind: "queued", ahead: 2 });
    expect(queueLabel(0)).toBe("queued: next");
    expect(queueLabel(2)).toBe("queued: 2 ahead");
    expect(questionStateLabel({ kind: "queued", ahead: 1 })).toBe("queued: 1 ahead");
  });

  it("says no answer yet when nothing is in flight for an unanswered question", () => {
    expect(questionStateLabel(questionState(question("q9"), EMPTY_LIVE))).toBe("no answer yet");
  });
});

describe("formatting", () => {
  it("formats the cost in dollars", () => {
    expect(formatCost(0.42)).toBe("$0.42");
    expect(formatCost(1.005)).toBe("$1.00");
    expect(formatCost(12.3)).toBe("$12.30");
    expect(formatCost(0.004)).toBe("<$0.01");
    expect(formatCost(0)).toBe("$0.00");
    expect(formatCost(null)).toBe("$0.00");
  });

  it("formats context tokens", () => {
    expect(formatTokens(null)).toBe("—");
    expect(formatTokens(950)).toBe("950 tokens");
    expect(formatTokens(12_000)).toBe("12k tokens");
    expect(formatTokens(48_250)).toBe("48.3k tokens");
    expect(formatTokens(1_250_000)).toBe("1.25M tokens");
  });
});

describe("composer modes", () => {
  const commentable: Commentable = { additions: [[18, 28]], deletions: [[18, 23]] };
  const range = (start: number, end: number) => ({ start, end, side: "additions" as const });

  it("opens in the preferred mode when the lines take it", () => {
    const comment = composerFor("a.py", range(20, 22), commentable, "comment", true);
    const ask = composerFor("a.py", range(20, 22), commentable, "question", true);

    expect(comment.ok && comment.composer.mode).toBe("comment");
    expect(ask.ok && ask.composer.mode).toBe("question");
    expect(ask.ok && ask.composer.comment).toMatchObject({ line: 22, start_line: 20 });
  });

  it("asks the agent about a line outside the diff, with no comment anchor", () => {
    const result = composerFor("a.py", range(5, 5), commentable, "comment", true);

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.composer.mode).toBe("question");
    expect(result.composer.comment).toBeNull();
    expect(composerAnchor(result.composer)).toEqual({ path: "a.py", side: "additions", line: 5 });
  });

  it("keeps the picked lines for a question and clamps them for a comment", () => {
    const result = composerFor("a.py", range(25, 31), commentable, "comment", true);

    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.composer.clamped).toBe(true);
    expect(result.composer.comment).toMatchObject({ start_line: 25, line: 28 });
    expect(result.composer.question).toMatchObject({ start_line: 25, line: 31 });
    expect(composerAnchor({ ...result.composer, mode: "question" })).toMatchObject({ line: 31 });
  });

  it("orders an upward pick", () => {
    const result = composerFor("a.py", range(9, 4), undefined, "question", true);

    expect(result.ok && result.composer.question).toEqual({
      path: "a.py",
      side: "additions",
      line: 9,
      start_line: 4,
      start_side: "additions",
    });
  });

  it("allows only comments when the agent is off", () => {
    expect(composerFor("a.py", range(5, 5), commentable, "question", false)).toEqual({
      ok: false,
      hint: expect.stringContaining("inside the diff"),
    });
    const inside = composerFor("a.py", range(20, 20), commentable, "question", false);
    expect(inside.ok && inside.composer.mode).toBe("comment");
  });
});

describe("thread placement", () => {
  it("moves inline threads on hidden unchanged lines above the diff", () => {
    const shown = question("q1", { line: 20 });
    const hidden = question("q2", { line: 3 });
    const commentable: Commentable = { additions: [[18, 28]], deletions: [] };

    expect(splitByHunks([shown, hidden], commentable)).toEqual({
      shown: [shown],
      hidden: [hidden],
    });
    expect(splitByHunks([hidden], undefined)).toEqual({ shown: [hidden], hidden: [] });
  });

  it("lists questions PR first, then by file order and line, without the overview", () => {
    const overview = question("ov", { path: "", line: 0, created_by: "agent" });
    const pr = question("pr", { path: "", line: 0 });
    const b = question("b", { path: "b.py", line: 3 });
    const a2 = question("a2", { path: "a.py", line: 9 });
    const a1 = question("a1", { path: "a.py", line: 0 });
    const comment = { ...question("c"), kind: "review_comment" as const, status: "draft" as const };

    expect(
      questionThreads([b, a2, comment, overview, a1, pr], ["a.py", "b.py"], null).map((t) => t.id),
    ).toEqual(["pr", "a1", "a2", "b"]);
  });
});
