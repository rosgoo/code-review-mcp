import { describe, expect, it } from "vitest";
import {
  EMPTY_LIVE,
  addStagedMessage,
  applyAgentEvent,
  applyAgentMessage,
  batchProgress,
  batchProgressLabel,
  composerAnchor,
  composerFor,
  editMessageBody,
  formatCost,
  formatTokens,
  markSent,
  markThreadsSent,
  ownsThread,
  questionState,
  questionStateLabel,
  questionThreads,
  removeMessage,
  splitByHunks,
  stagedThreadIds,
  statusFrom,
  withStatus,
  type AgentLive,
} from "./agent";
import type {
  AgentBatch,
  AgentStatus,
  Commentable,
  ReviewEvent,
  ReviewThread,
  ThreadMessage,
} from "./types";

function status(overrides: Partial<AgentStatus> = {}): AgentStatus {
  return {
    state: "idle",
    session_id: "s1",
    model: "claude-opus-5-5",
    cost_usd: 0,
    context_tokens: null,
    staged_count: 0,
    batch: null,
    partial: null,
    warmup: { status: "none", thread_id: null },
    ...overrides,
  };
}

const batch = (overrides: Partial<AgentBatch> = {}): AgentBatch => ({
  id: "b1",
  thread_ids: ["q1", "q2"],
  answered_ids: [],
  state: "running",
  ...overrides,
});

const userMessage = (id: string, body: string, staged = false): ThreadMessage => ({
  id,
  author: "user",
  body,
  created_at: "2026-10-05T12:00:00Z",
  status: staged ? "staged" : "sent",
});

const agentMessage = (id: string, body: string): ThreadMessage => ({
  id,
  author: "agent",
  body,
  created_at: "2026-10-05T12:01:00Z",
  status: "sent",
});

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
    messages: [userMessage(`${id}-m1`, "Why?")],
    ...overrides,
  };
}

const stagedQuestion = (id: string) =>
  question(id, { status: "draft", messages: [userMessage(`${id}-m1`, "Why?", true)] });

const run = (live: AgentLive, events: readonly ReviewEvent[]) =>
  events.reduce(applyAgentEvent, live);
const statusEvent = (overrides: Partial<AgentStatus>): ReviewEvent => ({
  type: "agent_status",
  ...status(overrides),
});

describe("staged questions", () => {
  it("counts each question thread with something staged once", () => {
    const followUps = question("q3", {
      messages: [
        userMessage("a", "Why?"),
        agentMessage("b", "Because."),
        userMessage("c", "And?", true),
        userMessage("d", "And then?", true),
      ],
    });
    const comment = { ...stagedQuestion("c1"), kind: "review_comment" as const };

    expect(stagedThreadIds([stagedQuestion("q1"), question("q2"), followUps, comment])).toEqual([
      "q1",
      "q3",
    ]);
  });

  it("adds, edits, and deletes a staged follow-up in its thread only", () => {
    const threads = [question("q1"), question("q2")];
    const followUp = userMessage("f1", "And?", true);

    const added = addStagedMessage(threads, "q2", followUp);
    expect(added[0]).toBe(threads[0]);
    expect(added[1]!.messages.map((m) => [m.id, m.status])).toEqual([
      ["q2-m1", "sent"],
      ["f1", "staged"],
    ]);

    const edited = editMessageBody(added, "q2", "f1", "And why not?");
    expect(edited[1]!.messages[1]).toMatchObject({ id: "f1", body: "And why not?", status: "staged" });
    expect(edited[1]!.messages[0]!.body).toBe("Why?");

    const removed = removeMessage(edited, "q2", "f1");
    expect(removed[1]!.messages.map((m) => m.id)).toEqual(["q2-m1"]);
    expect(questionState(removed[1]!, EMPTY_LIVE)).toEqual({ kind: "unanswered" });
  });

  it("marks only the sent threads' staged messages as sent and clears their old error", () => {
    const skipped = { ...stagedQuestion("q1"), agent_error: "The agent did not answer this question." };
    const other = stagedQuestion("q2");

    const [sent, untouched] = markThreadsSent([skipped, other], ["q1"]);

    expect(sent!.messages.map((m) => m.status)).toEqual(["sent"]);
    expect(sent!.agent_error).toBeNull();
    expect(untouched).toBe(other);
  });

  it("edits and deletes the whole thread only for an unsent first question", () => {
    const staged = stagedQuestion("q1");
    const followUp = question("q2", {
      messages: [userMessage("a", "Why?"), agentMessage("b", "Because."), userMessage("c", "And?", true)],
    });

    expect(ownsThread(staged, staged.messages[0]!)).toBe(true);
    expect(ownsThread(followUp, followUp.messages[2]!)).toBe(false);
    expect(ownsThread(followUp, followUp.messages[0]!)).toBe(false);
  });

  it("shows a staged thread as staged until it is sent", () => {
    const staged = stagedQuestion("q1");

    expect(questionState(staged, EMPTY_LIVE)).toEqual({ kind: "staged", error: null });
    expect(questionState(staged, markSent(EMPTY_LIVE, ["q1"]))).toEqual({ kind: "waiting" });
    expect(questionStateLabel({ kind: "staged", error: null })).toBe("staged");
  });
});

describe("batch progress reducer", () => {
  it("waits, streams, then lands each answer, counting the progress", () => {
    const q1 = question("q1");
    let live = run(markSent(EMPTY_LIVE, ["q1", "q2"]), [
      statusEvent({ state: "running", batch: batch() }),
    ]);
    expect(live.sent).toEqual({});
    expect(batchProgress(live.status)).toEqual({ total: 2, answered: 0, queued: false });
    expect(questionState(q1, live)).toEqual({ kind: "waiting" });
    expect(questionStateLabel({ kind: "waiting" })).toBe("waiting for the agent");

    live = run(live, [
      { type: "agent_delta", thread_id: "q1", text: "It " },
      { type: "agent_delta", thread_id: "q1", text: "returns early." },
    ]);
    expect(live.streams.q1).toBe("It returns early.");
    expect(questionState(q1, live)).toEqual({ kind: "streaming" });

    const answer = agentMessage("a1", "It returns early.");
    live = run(live, [
      { type: "agent_message", thread_id: "q1", message: answer },
      statusEvent({ state: "running", batch: batch({ answered_ids: ["q1"] }) }),
    ]);
    const [answered] = applyAgentMessage([q1], "q1", answer)!;
    expect(live.streams.q1).toBeUndefined();
    expect(questionState(answered!, live)).toEqual({ kind: "answered" });
    expect(questionState(question("q2"), live)).toEqual({ kind: "waiting" });
    expect(batchProgress(live.status)).toEqual({ total: 2, answered: 1, queued: false });

    live = run(live, [statusEvent({ state: "idle", batch: batch({ answered_ids: ["q1", "q2"], state: "done" }) })]);
    expect(batchProgress(live.status)).toBeNull();
  });

  it("labels a queued batch and counts only the threads it lists", () => {
    const queued = batchProgress(status({ state: "queued", batch: batch({ state: "queued" }) }))!;
    expect(queued).toEqual({ total: 2, answered: 0, queued: true });
    expect(batchProgressLabel(queued)).toBe("Queued: 2 questions");

    const running = batchProgress(
      status({ state: "running", batch: batch({ answered_ids: ["q1", "q9"] }) }),
    )!;
    expect(batchProgressLabel(running)).toBe("Answered 1 of 2");
    expect(batchProgress(status({ batch: batch({ state: "error" }) }))).toBeNull();
  });

  it("puts a skipped item back to staged with the reason", () => {
    let live = run(EMPTY_LIVE, [statusEvent({ state: "running", batch: batch() })]);
    live = run(live, [
      { type: "agent_error", thread_id: "q2", error: "The agent did not answer this question" },
      statusEvent({ state: "running", batch: batch({ answered_ids: ["q1"] }) }),
    ]);
    const skipped = stagedQuestion("q2");

    expect(questionState(skipped, live)).toEqual({
      kind: "staged",
      error: "The agent did not answer this question",
    });
    expect(questionStateLabel(questionState(skipped, live))).toBe("skipped, staged again");

    live = run(live, [statusEvent({ state: "running", batch: batch({ id: "b2", thread_ids: ["q2"] }) })]);
    expect(live.errors.q2).toBeUndefined();
    expect(questionState(skipped, live)).toEqual({ kind: "waiting" });
  });

  it("keeps answered items after a stop and returns the rest to staged", () => {
    const first = applyAgentMessage([question("q1")], "q1", agentMessage("a1", "Done."))![0]!;
    let live = run(EMPTY_LIVE, [
      statusEvent({ state: "running", batch: batch({ answered_ids: ["q1"] }) }),
      { type: "agent_delta", thread_id: "q2", text: "Half" },
    ]);
    live = run(live, [statusEvent({ state: "idle", batch: batch({ answered_ids: ["q1"], state: "stopped" }) })]);

    expect(live.streams.q2).toBeUndefined();
    expect(questionState(first, live)).toEqual({ kind: "answered" });
    expect(questionState(stagedQuestion("q2"), live)).toEqual({
      kind: "staged",
      error: null,
    });
    expect(batchProgress(live.status)).toBeNull();
  });

  it("fills the overview's first message in place", () => {
    const overview = question("ov", {
      path: "",
      line: 0,
      created_by: "agent",
      messages: [agentMessage("ov-m1", "")],
    });
    const filled = agentMessage("ov-m1", "## Overview");

    const [after] = applyAgentMessage([overview], "ov", filled)!;

    expect(after!.messages).toEqual([filled]);
    expect(applyAgentMessage([overview], "other", filled)).toBeNull();
  });

  it("ignores events that are not about the agent", () => {
    expect(applyAgentEvent(EMPTY_LIVE, { type: "view_updated" })).toBe(EMPTY_LIVE);
  });

  it("says no answer when a sent question has none and nothing is out", () => {
    expect(questionStateLabel(questionState(question("q9"), EMPTY_LIVE))).toBe("no answer");
  });
});

describe("question state rules", () => {
  const answered = question("q1", {
    messages: [userMessage("m1", "Why?"), agentMessage("a1", "Because.")],
  });
  const running = (overrides: Partial<AgentBatch> = {}) =>
    withStatus(EMPTY_LIVE, status({ state: "running", batch: batch({ thread_ids: ["q1"], ...overrides }) }));

  it("ranks streaming, then waiting, then staged, then error, then the answer", () => {
    const streaming = { ...running(), streams: { q1: "It" } };
    const skipped = applyAgentEvent(running(), {
      type: "agent_error",
      thread_id: "q1",
      error: "Timed out",
    });

    expect(questionState(answered, streaming)).toEqual({ kind: "streaming" });
    expect(questionState(answered, running())).toEqual({ kind: "waiting" });
    expect(questionState(stagedQuestion("q1"), skipped)).toEqual({ kind: "staged", error: "Timed out" });
    expect(questionState(question("q1"), skipped)).toEqual({ kind: "error", error: "Timed out" });
    expect(questionState(answered, running({ answered_ids: ["q1"] }))).toEqual({ kind: "answered" });
    expect(questionState(answered, running({ state: "done" }))).toEqual({ kind: "answered" });
    expect(questionState(question("q1"), EMPTY_LIVE)).toEqual({ kind: "unanswered" });
  });

  it("keeps a follow-up staged during a batch that does not carry it", () => {
    const followUp = question("q2", {
      messages: [userMessage("m1", "Why?"), agentMessage("a1", "Because."), userMessage("m2", "And?", true)],
    });

    expect(questionState(followUp, running())).toEqual({ kind: "staged", error: null });
  });
});

describe("partial answers", () => {
  it("fills in the answer so far for a tab that opens mid-answer", () => {
    const live = withStatus(
      EMPTY_LIVE,
      statusFrom(
        status({
          state: "running",
          batch: batch({ answered_ids: ["q1"] }),
          partial: { thread_id: "q2", text: "It returns" },
        }),
      ),
    );

    expect(live.streams).toEqual({ q2: "It returns" });
    expect(questionState(question("q2"), live)).toEqual({ kind: "streaming" });

    const next = applyAgentEvent(live, { type: "agent_delta", thread_id: "q2", text: " early." });
    expect(next.streams.q2).toBe("It returns early.");
  });

  it("keeps a longer stream and ignores a partial for a thread no longer being answered", () => {
    const streaming: AgentLive = {
      ...withStatus(EMPTY_LIVE, status({ state: "running", batch: batch() })),
      streams: { q1: "It returns early." },
    };
    const stale = status({
      state: "running",
      batch: batch(),
      partial: { thread_id: "q1", text: "It ret" },
    });
    expect(withStatus(streaming, stale).streams.q1).toBe("It returns early.");

    const answeredAlready = status({
      state: "running",
      batch: batch({ answered_ids: ["q1"] }),
      partial: { thread_id: "q1", text: "It returns early. And more" },
    });
    expect(withStatus(EMPTY_LIVE, answeredAlready).streams).toEqual({});

    const ended = status({ batch: batch({ state: "done" }), partial: { thread_id: "q2", text: "x" } });
    expect(withStatus(EMPTY_LIVE, ended).streams).toEqual({});
  });

  it("fills in the overview the warm-up is writing", () => {
    const live = withStatus(
      EMPTY_LIVE,
      status({
        state: "running",
        warmup: { status: "running", thread_id: "ov" },
        partial: { thread_id: "ov", text: "## Overview" },
      }),
    );

    expect(live.streams).toEqual({ ov: "## Overview" });
  });

  it("reads a status from a daemon that sends no partial or batch", () => {
    const bare = { state: "idle", warmup: { status: "none", thread_id: null } } as unknown as AgentStatus;

    expect(statusFrom(bare)).toMatchObject({ batch: null, partial: null, staged_count: 0 });
  });
});

describe("agent_error persistence", () => {
  const skipped = (id: string) => ({
    ...stagedQuestion(id),
    agent_error: "The agent did not answer this question.",
  });

  it("shows the thread's agent_error after a reload, with nothing in this tab", () => {
    expect(questionState(skipped("q1"), EMPTY_LIVE)).toEqual({
      kind: "staged",
      error: "The agent did not answer this question.",
    });
    expect(questionStateLabel(questionState(skipped("q1"), EMPTY_LIVE))).toBe(
      "skipped, staged again",
    );
  });

  it("waits for the agent once the thread is sent again, even before the thread reloads", () => {
    expect(questionState(skipped("q1"), markSent(EMPTY_LIVE, ["q1"]))).toEqual({ kind: "waiting" });

    const inBatch = withStatus(EMPTY_LIVE, status({ state: "running", batch: batch({ id: "b2" }) }));
    expect(questionState(skipped("q1"), inBatch)).toEqual({ kind: "waiting" });
  });

  it("clears the error when the answer lands", () => {
    const [answered] = applyAgentMessage([skipped("q1")], "q1", agentMessage("a1", "Fine."))!;

    expect(answered!.agent_error).toBeNull();
    expect(questionState(markThreadsSent([answered!], ["q1"])[0]!, EMPTY_LIVE)).toEqual({
      kind: "answered",
    });
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
