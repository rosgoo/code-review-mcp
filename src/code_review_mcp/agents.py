import asyncio
import contextlib
import logging
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NamedTuple, Protocol

from code_review_mcp.agent_prompts import (
    BatchItem,
    batch_prompt,
    pr_context,
    review_delta,
    review_instructions,
    warmup_prompt,
)
from code_review_mcp.answer_stream import AnswerStream
from code_review_mcp.config import Settings
from code_review_mcp.errors import ConflictError, NotFoundError, ReviewError
from code_review_mcp.github import GitHubClient
from code_review_mcp.hub import ReviewHub
from code_review_mcp.pr_service import PrService, ReadyPr
from code_review_mcp.repo_config import AgentConfig, ConfigError, RepoConfig
from code_review_mcp.review_threads import AnchorRequest, ThreadService, serialize_thread
from code_review_mcp.store import (
    MessageRow,
    ReviewRow,
    Side,
    Store,
    ThreadStatus,
    utc_now,
)

logger = logging.getLogger(__name__)

RECYCLE_FRACTION = 0.8
STOP_GRACE_SECONDS = 30.0
REAP_INTERVAL_SECONDS = 60.0
OVERVIEW_PATH = ""
ANSWER_TOOL = "mcp__review__answer_question"
NOT_ANSWERED = "The agent did not answer this question."
DAEMON_STOPPED = "The daemon stopped before the agent answered this question."

JobKind = Literal["batch", "warmup"]
AgentState = Literal["idle", "queued", "running", "error", "off"]
WarmupStatus = Literal["none", "running", "done", "error"]
BatchState = Literal["queued", "running", "done", "stopped", "error"]


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolInputDelta:
    """A chunk of the JSON input of a tool call; `block` identifies the call in the turn."""

    tool: str
    block: str
    partial_json: str


@dataclass(frozen=True)
class TurnEnd:
    result_text: str | None
    total_cost_usd: float | None
    session_id: str | None
    is_error: bool
    subtype: str
    aborted: bool


AgentEvent = TextDelta | ToolInputDelta | TurnEnd


class AgentSession(Protocol):
    """One live agent client: a conversation that answers one prompt at a time."""

    async def send(self, prompt: str) -> None: ...

    def events(self) -> AsyncIterator[AgentEvent]: ...

    async def interrupt(self) -> None: ...

    async def close(self) -> None: ...

    async def context_tokens(self) -> int | None: ...


class ToolOutcome(NamedTuple):
    ok: bool
    text: str


ToolHandler = Callable[[Mapping[str, Any]], Awaitable[ToolOutcome]]


class _Drained(NamedTuple):
    end: TurnEnd
    text: str
    timed_out: bool


@dataclass(frozen=True)
class SessionSpec:
    review_id: str
    cwd: Path
    model: str
    session_id: str
    resume: bool
    max_turns: int
    max_budget_usd: float
    instructions: str
    draft: ToolHandler
    answer: ToolHandler


SessionFactory = Callable[[SessionSpec], Awaitable[AgentSession]]


class AgentFailure(ReviewError):
    pass


def scrub_agent_env(environ: MutableMapping[str, str]) -> list[str]:
    """Remove the variables that would make an agent client act as a nested Claude Code run.

    Returns the removed names.
    """
    removed = [
        name
        for name in list(environ)
        if name == "CLAUDECODE" or name.startswith("CLAUDE_CODE_") or name == "NODE_EXTRA_CA_CERTS"
    ]
    for name in removed:
        del environ[name]
    return removed


@dataclass
class _BatchItem:
    thread_id: str
    message_ids: list[str]
    previous_status: ThreadStatus
    answered: bool = False


@dataclass
class _Batch:
    id: str
    items: list[_BatchItem]
    state: BatchState = "queued"

    def item(self, thread_id: str) -> _BatchItem | None:
        return next((i for i in self.items if i.thread_id == thread_id), None)

    def json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "thread_ids": [i.thread_id for i in self.items],
            "answered_ids": [i.thread_id for i in self.items if i.answered],
            "state": self.state,
        }


@dataclass
class _Partial:
    thread_id: str
    block: str
    parts: list[str] = field(default_factory=list)


@dataclass
class _Job:
    kind: JobKind
    thread_id: str | None = None
    batch: _Batch | None = None

    @property
    def thread_ids(self) -> list[str]:
        if self.batch is not None:
            return [i.thread_id for i in self.batch.items]
        return [self.thread_id] if self.thread_id else []


@dataclass
class _ReviewAgent:
    review_id: str
    jobs: deque[_Job] = field(default_factory=deque)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    worker: asyncio.Task[None] | None = None
    session: AgentSession | None = None
    session_fresh: bool = False
    session_cost: float = 0.0
    last_used: float = 0.0
    running: _Job | None = None
    stop_requested: bool = False
    error: str | None = None
    context_tokens: int | None = None
    warmup_status: WarmupStatus | None = None
    warmup_thread_id: str | None = None
    latest_batch: _Batch | None = None
    answering_thread_id: str | None = None
    streams: dict[str, AnswerStream] = field(default_factory=dict)
    partial: _Partial | None = None
    turn_sent: bool = False

    def held_thread_ids(self) -> set[str]:
        """Threads of the running turn and of every queued turn."""
        held = {tid for job in self.jobs for tid in job.thread_ids}
        if self.running is not None:
            held.update(self.running.thread_ids)
        return held

    def waiting_thread_ids(self) -> set[str]:
        """Threads that wait for an answer: queued, or unanswered in the running batch."""
        waiting = {tid for job in self.jobs for tid in job.thread_ids}
        if self.running is not None and self.running.batch is not None:
            waiting.update(i.thread_id for i in self.running.batch.items if not i.answered)
        return waiting


def _anchor_side(value: object) -> Side | None:
    if value in ("additions", "RIGHT", "right"):
        return "additions"
    if value in ("deletions", "LEFT", "left"):
        return "deletions"
    return None


def _message_json(message: MessageRow) -> dict[str, object]:
    return {
        "id": message.id,
        "author": message.author,
        "body": message.body,
        "created_at": message.created_at,
        "status": message.status,
    }


class AgentRunner:
    """Answers staged questions with one agent session per PR review.

    The user stages questions and follow-ups, then sends them; each send is one batch turn
    in which the agent answers every item with answer_question. Turns on one review run one
    at a time, in order; different reviews run in parallel. At most `max_live_clients`
    sessions stay open; the least recently used idle one closes first. A session closes
    after `idle_minutes`, when the review's worktree is released or removed, and on
    shutdown. The next turn resumes the stored session.
    """

    def __init__(
        self,
        store: Store,
        hub: ReviewHub,
        settings: Settings,
        prs: PrService,
        threads: ThreadService,
        github: GitHubClient,
        config_loader: Callable[[], RepoConfig],
        session_factory: SessionFactory,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._hub = hub
        self._settings = settings
        self._prs = prs
        self._threads = threads
        self._github = github
        self._config_loader = config_loader
        self._factory = session_factory
        self._clock = clock
        self._agents: dict[str, _ReviewAgent] = {}
        self._capacity = asyncio.Condition()
        self._background: set[asyncio.Task[None]] = set()
        prs.on_pr_opened(self._pr_opened)
        prs.on_worktree_removed(self._worktree_removed)
        threads.on_staged_change(self._publish_status)
        self._recover()

    def _recover(self) -> None:
        """Stage again the questions that a stopped daemon left without an answer."""
        for thread_id, message_ids in self._store.unanswered_questions().items():
            first = self._store.list_messages(thread_id)[0]
            self._store.restage_question(
                thread_id,
                message_ids,
                status="draft" if first.id in message_ids else "submitted",
                agent_error=DAEMON_STOPPED,
            )
            logger.info("staged question thread %s again after a restart", thread_id)

    def _config(self) -> AgentConfig:
        try:
            return self._config_loader().agent
        except ConfigError as e:
            logger.warning("agent config is invalid, using defaults: %s", e)
            return AgentConfig()

    def enabled(self) -> bool:
        return self._settings.agent_enabled and self._config().enabled

    def require_on(self) -> None:
        """Raise ConflictError when the agent is off (config or CODE_REVIEW_MCP_AGENT=0)."""
        if not self.enabled():
            raise ConflictError("The review agent is off")

    def _agent(self, review_id: str) -> _ReviewAgent:
        agent = self._agents.get(review_id)
        if agent is None:
            agent = _ReviewAgent(review_id=review_id, last_used=self._clock())
            self._agents[review_id] = agent
        return agent

    def _spawn(self, coro: Awaitable[None]) -> None:
        async def run() -> None:
            await coro

        task = asyncio.create_task(run())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _overview_thread_id(self, review_id: str) -> str | None:
        return next(
            (
                t.id
                for t in self._store.list_threads(review_id, kind="question")
                if t.path == OVERVIEW_PATH and t.line == 0 and t.created_by == "agent"
            ),
            None,
        )

    def _staged(self, review_id: str) -> dict[str, list[MessageRow]]:
        """Question threads with staged messages, in thread order, with those messages."""
        messages = self._store.messages_for_review(review_id)
        staged: dict[str, list[MessageRow]] = {}
        for thread in self._store.list_threads(review_id, kind="question"):
            pending = [m for m in messages.get(thread.id, []) if m.status == "staged"]
            if pending:
                staged[thread.id] = pending
        return staged

    def status(self, review_id: str) -> dict[str, object]:
        """The review's agent state, as GET /agent and SSE agent_status carry it."""
        review = self._prs.require_pr_review(review_id)
        agent = self._agents.get(review_id) or _ReviewAgent(review_id=review_id)
        state: AgentState
        if not self.enabled():
            state = "off"
        elif agent.running is not None:
            state = "running"
        elif agent.jobs:
            state = "queued"
        elif agent.error is not None:
            state = "error"
        else:
            state = "idle"
        overview_id = self._overview_thread_id(review_id)
        warmup_status: WarmupStatus = agent.warmup_status or ("done" if overview_id else "none")
        running = agent.running
        batch = running.batch if running is not None and running.batch else agent.latest_batch
        partial = agent.partial
        return {
            "state": state,
            "session_id": review.agent_session_id,
            "model": self._config().model,
            "cost_usd": review.agent_cost_usd,
            "context_tokens": agent.context_tokens,
            "staged_count": len(self._staged(review_id)),
            "queue": [tid for job in agent.jobs for tid in job.thread_ids],
            "running_thread_id": (
                running.thread_id
                if running is not None and running.kind == "warmup"
                else agent.answering_thread_id
            ),
            "batch": batch.json() if batch is not None else None,
            "partial": (
                {"thread_id": partial.thread_id, "text": "".join(partial.parts)}
                if partial is not None
                else None
            ),
            "warmup": {
                "status": warmup_status,
                "thread_id": agent.warmup_thread_id or overview_id,
            },
        }

    def _publish_status(self, review_id: str) -> None:
        self._hub.publish(review_id, "agent_status", self.status(review_id))

    def _publish_thread(self, review_id: str, thread_id: str) -> None:
        if self._store.get_thread(thread_id) is not None:
            self._hub.publish(
                review_id, "thread_updated", {"thread": self._threads.thread_json(thread_id)}
            )

    def _enqueue(self, review_id: str, job: _Job) -> None:
        agent = self._agent(review_id)
        agent.jobs.append(job)
        if agent.worker is None or agent.worker.done():
            agent.worker = asyncio.create_task(self._work(agent))
        self._publish_status(review_id)

    def send(self, review_id: str, thread_ids: Sequence[str] | None = None) -> dict[str, object]:
        """Send staged questions and follow-ups as one batch turn.

        Without `thread_ids`, every thread with staged messages goes, except a thread that
        still waits for an earlier answer. Marks those messages sent, and the threads
        submitted with no agent error. Raises ConflictError when the agent is off, nothing
        is staged, or a named thread waits for an answer; NotFoundError for a thread
        outside the review.
        """
        self.require_on()
        review = self._prs.require_pr_review(review_id)
        if review.status == "closed":
            raise ReviewError(f"Review {review_id!r} is closed. Open the PR again to ask.")
        staged = self._staged(review_id)
        agent = self._agents.get(review_id)
        waiting = agent.waiting_thread_ids() if agent is not None else set()
        if thread_ids is None:
            chosen = [tid for tid in staged if tid not in waiting]
        else:
            chosen = list(dict.fromkeys(thread_ids))
            for thread_id in chosen:
                thread = self._store.get_thread(thread_id)
                if thread is None or thread.review_id != review_id:
                    raise NotFoundError(f"Thread {thread_id!r} not found in this review")
                if thread_id not in staged:
                    raise ConflictError(f"Thread {thread_id!r} has nothing staged to send")
                if thread_id in waiting:
                    raise ConflictError(
                        f"Thread {thread_id!r} waits for an answer; send it after that answer"
                    )
        if not chosen:
            raise ConflictError("Nothing is staged to send")
        items: list[_BatchItem] = []
        for thread_id in chosen:
            thread = self._store.get_thread(thread_id)
            assert thread is not None
            message_ids = [m.id for m in staged[thread_id]]
            self._store.mark_question_sent(thread_id, message_ids)
            items.append(_BatchItem(thread_id, message_ids, thread.status))
            self._publish_thread(review_id, thread_id)
        batch = _Batch(id=uuid.uuid4().hex, items=items)
        self._agent(review_id).latest_batch = batch
        self._enqueue(review_id, _Job(kind="batch", batch=batch))
        return {"batch_id": batch.id, "thread_ids": chosen}

    async def stop(self, review_id: str) -> dict[str, object]:
        """Interrupt the running turn (a batch or the warm-up) and cancel every queued batch.

        The worker drains the interrupted turn to its end. Answered items keep their
        answers; every other item goes back to staged, and those batches end `stopped`.
        Raises ConflictError when no turn runs and no batch waits.
        """
        self._prs.require_pr_review(review_id)
        agent = self._agents.get(review_id)
        queued = [j for j in agent.jobs if j.batch is not None] if agent is not None else []
        if agent is None or (agent.running is None and not queued):
            raise ConflictError("No agent turn is running or queued for this review")
        cancelled: list[str] = []
        for job in queued:
            assert job.batch is not None
            agent.jobs.remove(job)
            job.batch.state = "stopped"
            cancelled.append(job.batch.id)
            for item in job.batch.items:
                self._restage(review_id, item, None)
        interrupted = agent.running is not None
        if interrupted:
            agent.stop_requested = True
            if agent.turn_sent and agent.session is not None:
                await agent.session.interrupt()
        self._publish_status(review_id)
        return {"interrupted": interrupted, "cancelled_batch_ids": cancelled}

    def delete_question(self, thread_id: str) -> None:
        """Delete a question thread in any status, with its messages.

        Raises NotFoundError for an unknown thread, ReviewError for a thread of another
        kind, and ConflictError while a queued or running turn holds the thread.
        """
        thread = self._store.get_thread(thread_id)
        if thread is None:
            raise NotFoundError(f"Thread {thread_id!r} not found")
        if thread.kind != "question":
            raise ReviewError(f"Thread {thread_id!r} is not a question thread")
        agent = self._agents.get(thread.review_id)
        if agent is not None and thread_id in agent.held_thread_ids():
            raise ConflictError(
                f"Thread {thread_id!r} is in a queued or running agent turn. Stop the agent first."
            )
        self._store.delete_thread(thread_id)
        if agent is not None and agent.warmup_thread_id == thread_id:
            agent.warmup_thread_id = None
            agent.warmup_status = None
        self._hub.publish(
            thread.review_id, "thread_deleted", {"comment_id": thread_id, "thread_id": thread_id}
        )
        self._publish_status(thread.review_id)

    async def start_warmup(self, review_id: str) -> dict[str, object]:
        """Create the overview thread and queue the warm-up turn.

        Raises ConflictError when the agent is off, or a warm-up exists or is running. A
        failed warm-up can start again.
        """
        self.require_on()
        review = self._prs.require_pr_review(review_id)
        if review.status == "closed":
            raise ReviewError(f"Review {review_id!r} is closed. Open the PR again.")
        agent = self._agent(review_id)
        if agent.warmup_status == "running" or any(j.kind == "warmup" for j in agent.jobs):
            raise ConflictError("A warm-up is already running for this review")
        existing = self._overview_thread_id(review_id)
        if existing is not None:
            if agent.warmup_status != "error":
                raise ConflictError("This review already has an overview")
            self._store.delete_thread(existing)
            self._hub.publish(
                review_id, "thread_deleted", {"comment_id": existing, "thread_id": existing}
            )
        thread = self._store.create_thread(
            review_id=review_id,
            kind="question",
            path=OVERVIEW_PATH,
            side="additions",
            line=0,
            status="submitted",
            author="agent",
            body="",
            anchor_sha=review.head_sha,
        )
        agent.warmup_status = "running"
        agent.warmup_thread_id = thread.id
        self._hub.publish(
            review_id,
            "thread_added",
            {"thread": serialize_thread(thread, self._store.list_messages(thread.id))},
        )
        self._enqueue(review_id, _Job(kind="warmup", thread_id=thread.id))
        return self.status(review_id)

    def _pr_opened(self, review: ReviewRow, created: bool) -> None:
        if created and self.enabled():
            self._spawn(self._maybe_warm_up(review))

    async def _maybe_warm_up(self, review: ReviewRow) -> None:
        mode = self._config().warmup
        if mode == "never":
            return
        if mode == "inbox":
            try:
                direct = (await self._github.inbox_list("direct")).inbox_list
            except ReviewError as e:
                logger.warning("warm-up skipped for %s#%s: %s", review.repo, review.pr_number, e)
                return
            if not any(
                pr.repo == review.repo and pr.number == review.pr_number for pr in direct.items
            ):
                return
        try:
            await self.start_warmup(review.id)
        except ReviewError as e:
            logger.info("warm-up skipped for %s#%s: %s", review.repo, review.pr_number, e)

    def _worktree_removed(self, review_id: str) -> None:
        if review_id in self._agents:
            self._spawn(self.close_session(review_id, stop_running=True))

    async def close_session(self, review_id: str, *, stop_running: bool = False) -> None:
        """Close the review's live client, if any. With `stop_running`, a running turn is
        interrupted first; otherwise it finishes before the close."""
        agent = self._agents.get(review_id)
        if agent is None:
            return
        if stop_running and agent.running is not None and agent.session is not None:
            agent.stop_requested = True
            with contextlib.suppress(Exception):
                await agent.session.interrupt()
        async with agent.lock:
            await self._close(agent)

    async def _close(self, agent: _ReviewAgent) -> None:
        session, agent.session = agent.session, None
        agent.session_cost = 0.0
        if session is None:
            return
        try:
            await session.close()
        except Exception:  # a client that fails to close must not block the next one
            logger.exception("closing the agent client of review %s failed", agent.review_id)
        async with self._capacity:
            self._capacity.notify_all()

    async def reap_idle(self) -> list[str]:
        """Close every client idle for longer than `idle_minutes`. Returns their review ids."""
        limit = self._config().idle_minutes * 60
        closed: list[str] = []
        for agent in list(self._agents.values()):
            if agent.session is None or agent.lock.locked() or agent.jobs:
                continue
            if self._clock() - agent.last_used >= limit:
                async with agent.lock:
                    await self._close(agent)
                closed.append(agent.review_id)
                logger.info("closed the idle agent client of review %s", agent.review_id)
        return closed

    async def run_reaper(self, interval: float = REAP_INTERVAL_SECONDS) -> None:
        while True:
            await asyncio.sleep(interval)
            await self.reap_idle()

    async def shutdown(self) -> None:
        """Stop every worker and close every client. Unanswered items go back to staged."""
        for agent in self._agents.values():
            while agent.jobs:
                self._abandon(agent, agent.jobs.popleft())
        tasks = [a.worker for a in self._agents.values() if a.worker and not a.worker.done()]
        tasks += list(self._background)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for agent in self._agents.values():
            await self._close(agent)

    async def wait_idle(self, review_id: str) -> None:
        """Wait until the review has no queued or running agent turn."""
        agent = self._agents.get(review_id)
        while agent is not None and agent.worker is not None and not agent.worker.done():
            await asyncio.shield(agent.worker)

    def live_clients(self) -> int:
        return sum(1 for a in self._agents.values() if a.session is not None)

    async def _make_room(self, agent: _ReviewAgent) -> None:
        limit = self._config().max_live_clients
        while True:
            async with self._capacity:
                if self.live_clients() < limit:
                    return
                idle = [
                    a
                    for a in self._agents.values()
                    if a.session is not None and a is not agent and a.running is None
                ]
                if not idle:
                    await self._capacity.wait()
                    continue
                oldest = min(idle, key=lambda a: a.last_used)
            logger.info("closing the agent client of review %s for room", oldest.review_id)
            async with oldest.lock:
                await self._close(oldest)

    async def _session(self, agent: _ReviewAgent, pr: ReadyPr) -> AgentSession:
        if agent.session is not None:
            return agent.session
        await self._make_room(agent)
        config = self._config()
        review = pr.review
        session_id = review.agent_session_id
        resume = session_id is not None and review.agent_last_turn_at is not None
        if session_id is None:
            session_id = str(uuid.uuid4())
            self._store.set_agent_session_id(review.id, session_id)
        assert review.repo is not None and review.pr_number is not None
        spec = SessionSpec(
            review_id=review.id,
            cwd=pr.repo_dir,
            model=config.model,
            session_id=session_id,
            resume=resume,
            max_turns=config.max_turns,
            max_budget_usd=config.max_client_budget_usd,
            instructions=review_instructions(review.repo, review.pr_number),
            draft=lambda args: self._draft(review.id, args),
            answer=lambda args: self._answer(review.id, args),
        )
        agent.session = await self._factory(spec)
        agent.session_fresh = not resume
        agent.session_cost = 0.0
        return agent.session

    async def _draft(self, review_id: str, args: Mapping[str, Any]) -> ToolOutcome:
        path = args.get("path")
        line = args.get("line")
        body = args.get("body")
        start_line = args.get("start_line")
        if not isinstance(path, str) or not isinstance(line, int) or not isinstance(body, str):
            return ToolOutcome(False, "path (string), line (integer), and body are required")
        try:
            thread = await self._threads.create_thread(
                review_id,
                path,
                AnchorRequest(
                    _anchor_side(args.get("side", "additions")),
                    line,
                    start_line if isinstance(start_line, int) and start_line > 0 else None,
                    _anchor_side(args.get("start_side")),
                ),
                body,
                created_by="agent",
            )
        except ReviewError as e:
            return ToolOutcome(False, f"The draft was not saved: {e}")
        return ToolOutcome(
            True,
            f"Draft review comment {thread['id']} saved on {path} line {thread['line']}. The "
            "user reviews it before anything is posted.",
        )

    async def _answer(self, review_id: str, args: Mapping[str, Any]) -> ToolOutcome:
        thread_id = args.get("thread_id")
        answer = args.get("answer")
        if not isinstance(thread_id, str) or not isinstance(answer, str):
            return ToolOutcome(False, "thread_id and answer (strings) are required")
        agent = self._agents.get(review_id)
        running = agent.running if agent is not None else None
        batch = running.batch if running is not None else None
        if agent is None or batch is None:
            return ToolOutcome(False, "No questions are waiting for an answer.")
        item = batch.item(thread_id)
        if item is None:
            listed = ", ".join(i.thread_id for i in batch.items)
            return ToolOutcome(False, f"Thread {thread_id} is not in this batch ({listed}).")
        if item.answered:
            return ToolOutcome(False, f"Thread {thread_id} already has an answer.")
        if not answer.strip():
            return ToolOutcome(False, "The answer is empty.")
        self._store_answer(agent, item, answer.strip())
        left = [i.thread_id for i in batch.items if not i.answered]
        return ToolOutcome(
            True,
            f"Saved the answer for thread {thread_id}. "
            + (f"Still unanswered: {', '.join(left)}." if left else "Every item is answered."),
        )

    def _store_answer(self, agent: _ReviewAgent, item: _BatchItem, body: str) -> None:
        message = self._store.add_message(item.thread_id, author="agent", body=body)
        item.answered = True
        if agent.answering_thread_id == item.thread_id:
            agent.answering_thread_id = None
        if agent.partial is not None and agent.partial.thread_id == item.thread_id:
            agent.partial = None
        self._hub.publish(
            agent.review_id,
            "agent_message",
            {"thread_id": item.thread_id, "message": _message_json(message)},
        )
        self._publish_status(agent.review_id)

    async def _work(self, agent: _ReviewAgent) -> None:
        while agent.jobs:
            async with agent.lock:
                if not agent.jobs:
                    break
                job = agent.jobs.popleft()
                agent.running = job
                agent.stop_requested = False
                agent.turn_sent = False
                agent.answering_thread_id = None
                agent.partial = None
                agent.streams = {}
                if job.batch is not None:
                    job.batch.state = "running"
                self._publish_status(agent.review_id)
                try:
                    await self._run(agent, job)
                except (ReviewError, OSError) as e:
                    agent.error = str(e)
                    logger.warning("agent turn on review %s failed: %s", agent.review_id, e)
                    self._turn_failed(agent, job, str(e))
                except asyncio.CancelledError:
                    self._abandon(agent, job)
                    raise
                finally:
                    agent.running = None
                    agent.answering_thread_id = None
                    agent.partial = None
                    agent.last_used = self._clock()
            async with self._capacity:
                self._capacity.notify_all()
            self._publish_status(agent.review_id)
        agent.worker = None

    def _turn_failed(self, agent: _ReviewAgent, job: _Job, error: str) -> None:
        if job.batch is not None:
            job.batch.state = "error"
            for item in job.batch.items:
                if not item.answered:
                    self._restage(agent.review_id, item, error)
            return
        agent.warmup_status = "error"
        if job.thread_id is not None and self._store.get_thread(job.thread_id) is not None:
            self._store.set_agent_error(job.thread_id, error)
        self._hub.publish(
            agent.review_id, "agent_error", {"thread_id": job.thread_id, "error": error}
        )

    def _abandon(self, agent: _ReviewAgent, job: _Job) -> None:
        """End a job the daemon will not finish: its unanswered items go back to staged, and
        a warm-up that wrote nothing loses its empty overview."""
        if job.batch is not None:
            job.batch.state = "stopped"
            for item in job.batch.items:
                if not item.answered:
                    self._restage(agent.review_id, item, DAEMON_STOPPED)
        else:
            self._drop_warmup(agent, job)

    def _drop_warmup(self, agent: _ReviewAgent, job: _Job) -> None:
        thread_id = job.thread_id
        if thread_id is None or self._store.get_thread(thread_id) is None:
            return
        if any(m.body for m in self._store.list_messages(thread_id)):
            return
        self._store.delete_thread(thread_id)
        if agent.warmup_thread_id == thread_id:
            agent.warmup_thread_id = None
            agent.warmup_status = None
        self._hub.publish(
            agent.review_id, "thread_deleted", {"comment_id": thread_id, "thread_id": thread_id}
        )

    def _restage(self, review_id: str, item: _BatchItem, error: str | None) -> None:
        """Stage the item's messages again, restore its thread status, and record `error`
        on the thread (published as agent_error when set)."""
        if self._store.get_thread(item.thread_id) is None:
            return
        self._store.restage_question(
            item.thread_id, item.message_ids, status=item.previous_status, agent_error=error
        )
        self._publish_thread(review_id, item.thread_id)
        if error is not None:
            self._hub.publish(
                review_id, "agent_error", {"thread_id": item.thread_id, "error": error}
            )

    def _batch_items(self, batch: _Batch) -> list[BatchItem]:
        items: list[BatchItem] = []
        for entry in list(batch.items):
            thread = self._store.get_thread(entry.thread_id)
            if thread is None:
                batch.items.remove(entry)
                continue
            messages = self._store.list_messages(entry.thread_id)
            sent_ids = set(entry.message_ids)
            items.append(
                BatchItem(
                    thread=thread,
                    earlier=[m for m in messages if m.status == "sent" and m.id not in sent_ids],
                    sent=[m for m in messages if m.id in sent_ids],
                )
            )
        return items

    async def _prompt(self, agent: _ReviewAgent, job: _Job, pr: ReadyPr, review: ReviewRow) -> str:
        files = await self._prs.changed_files(pr)
        head_changes = None
        if review.agent_head_sha and review.head_sha and review.agent_head_sha != review.head_sha:
            head_changes = await self._prs.files_between(pr, review.agent_head_sha, review.head_sha)
        assert review.head_sha is not None
        delta = review_delta(
            review,
            self._store.list_threads(review.id),
            self._store.messages_for_review(review.id),
            self._store.viewed_since(review.id, review.head_sha, review.agent_last_turn_at),
            exclude_thread_ids=set(job.thread_ids),
            head_changes=head_changes,
        )
        context = pr_context(review, files)
        if job.batch is None:
            return warmup_prompt(review, context, delta)
        return batch_prompt(
            review,
            self._batch_items(job.batch),
            context=context if agent.session_fresh else None,
            delta=delta,
        )

    async def _run(self, agent: _ReviewAgent, job: _Job) -> None:
        config = self._config()
        pr = await self._prs.ready_pr(agent.review_id)
        session = await self._session(agent, pr)
        review = self._prs.require_pr_review(agent.review_id)
        prompt = await self._prompt(agent, job, pr, review)
        if job.batch is not None and not job.batch.items:
            job.batch.state = "done"
            return
        if agent.stop_requested:
            self._stopped_before_send(agent, job)
            return
        await session.send(prompt)
        agent.turn_sent = True
        if agent.stop_requested:
            await session.interrupt()
        agent.session_fresh = False
        drained = await self._drain(agent, session, job, config.question_timeout_minutes * 60)
        self._store.record_agent_turn(agent.review_id, at=utc_now(), head_sha=pr.head_sha)
        await self._account(agent, drained.end, config)
        if job.batch is not None:
            self._finish_batch(agent, job.batch, drained, config)
        else:
            self._finish_warmup(agent, job, drained, config)

    def _stopped_before_send(self, agent: _ReviewAgent, job: _Job) -> None:
        if job.batch is not None:
            job.batch.state = "stopped"
            for item in job.batch.items:
                self._restage(agent.review_id, item, None)
        else:
            self._drop_warmup(agent, job)

    def _on_tool_input(self, agent: _ReviewAgent, job: _Job, event: ToolInputDelta) -> None:
        if event.tool != ANSWER_TOOL or job.batch is None:
            return
        stream = agent.streams.setdefault(event.block, AnswerStream())
        text = stream.feed(event.partial_json)
        item = job.batch.item(stream.thread_id) if stream.thread_id else None
        if item is None or item.answered:
            return
        if agent.partial is None or agent.partial.block != event.block:
            agent.partial = _Partial(item.thread_id, event.block)
            agent.answering_thread_id = item.thread_id
            self._publish_status(agent.review_id)
        if text:
            agent.partial.parts.append(text)
            self._hub.publish(
                agent.review_id, "agent_delta", {"thread_id": item.thread_id, "text": text}
            )

    async def _drain(
        self, agent: _ReviewAgent, session: AgentSession, job: _Job, timeout: float
    ) -> _Drained:
        deltas: list[str] = []
        end: TurnEnd | None = None
        timed_out = asyncio.Event()

        async def watchdog() -> None:
            await asyncio.sleep(timeout)
            timed_out.set()
            await session.interrupt()
            await asyncio.sleep(STOP_GRACE_SECONDS)
            logger.warning("agent turn on review %s did not stop; closing it", agent.review_id)
            await session.close()

        guard = asyncio.create_task(watchdog())
        try:
            async for event in session.events():
                if isinstance(event, TextDelta):
                    deltas.append(event.text)
                    if job.kind == "warmup" and job.thread_id is not None:
                        if agent.partial is None:
                            agent.partial = _Partial(job.thread_id, "text")
                        agent.partial.parts.append(event.text)
                        self._hub.publish(
                            agent.review_id,
                            "agent_delta",
                            {"thread_id": job.thread_id, "text": event.text},
                        )
                elif isinstance(event, ToolInputDelta):
                    self._on_tool_input(agent, job, event)
                else:
                    end = event
                    break
        finally:
            guard.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await guard
        partial = "".join(deltas)
        if end is None:
            await self._close(agent)
            raise AgentFailure(
                "The agent client stopped without finishing the turn"
                + (f". Partial text: {partial[:200]}" if partial else "")
            )
        return _Drained(end=end, text=partial, timed_out=timed_out.is_set())

    async def _account(self, agent: _ReviewAgent, end: TurnEnd, config: AgentConfig) -> None:
        if end.total_cost_usd is not None:
            spent = max(0.0, end.total_cost_usd - agent.session_cost)
            agent.session_cost = end.total_cost_usd
            self._store.add_agent_cost(agent.review_id, spent)
        if agent.session is not None:
            with contextlib.suppress(Exception):
                agent.context_tokens = await agent.session.context_tokens()
        if agent.session is not None and (
            end.subtype == "error_max_budget_usd"
            or agent.session_cost >= RECYCLE_FRACTION * config.max_client_budget_usd
        ):
            logger.info("recycling the agent client of review %s", agent.review_id)
            await self._close(agent)

    def _finish_batch(
        self, agent: _ReviewAgent, batch: _Batch, drained: _Drained, config: AgentConfig
    ) -> None:
        end = drained.end
        stopped = drained.timed_out or agent.stop_requested or end.aborted
        text = (end.result_text or drained.text).strip()
        unanswered = [i for i in batch.items if not i.answered]
        if len(batch.items) == 1 and unanswered and text and not stopped and not end.is_error:
            self._store_answer(agent, unanswered[0], text)
            unanswered = []
        elif text:
            logger.info(
                "dropped text the agent wrote outside answer_question in batch %s: %s",
                batch.id,
                text[:500],
            )
        turn_error = f"The agent turn ended with {end.subtype}"
        error: str | None
        if drained.timed_out:
            error = (
                f"The agent stopped after {config.question_timeout_minutes:g} minutes before "
                "answering this question."
            )
        elif stopped:
            error = None
        elif end.is_error:
            error = turn_error
        else:
            error = NOT_ANSWERED
        for item in unanswered:
            self._restage(agent.review_id, item, error)
        if drained.timed_out or (end.is_error and not stopped):
            batch.state = "error"
            agent.error = error if drained.timed_out else turn_error
        elif stopped:
            batch.state = "stopped"
            agent.error = None
        else:
            batch.state = "done"
            agent.error = None

    def _finish_warmup(
        self, agent: _ReviewAgent, job: _Job, drained: _Drained, config: AgentConfig
    ) -> None:
        end = drained.end
        assert job.thread_id is not None
        stopped = drained.timed_out or agent.stop_requested or end.aborted
        if drained.timed_out:
            note = f"_(stopped after {config.question_timeout_minutes:g} minutes)_"
            body = f"{drained.text}\n\n{note}".strip()
        elif stopped:
            body = f"{drained.text}\n\n_(stopped)_".strip()
        elif end.is_error:
            body = drained.text.strip()
        else:
            body = (end.result_text or drained.text).strip()
        if body:
            self._store.update_first_message(job.thread_id, body)
            first = self._store.list_messages(job.thread_id)[0]
            self._hub.publish(
                agent.review_id,
                "agent_message",
                {"thread_id": job.thread_id, "message": _message_json(first)},
            )
        if end.is_error and not stopped:
            agent.warmup_status = "error"
            agent.error = f"The agent turn ended with {end.subtype}"
            self._store.set_agent_error(job.thread_id, agent.error)
            self._hub.publish(
                agent.review_id,
                "agent_error",
                {"thread_id": job.thread_id, "error": agent.error},
            )
        else:
            agent.warmup_status = "done"
            agent.error = None
