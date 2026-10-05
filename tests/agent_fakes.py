import asyncio
import contextlib
import json
import re
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from code_review_mcp.agents import (
    ANSWER_TOOL,
    AgentEvent,
    SessionSpec,
    TextDelta,
    ToolInputDelta,
    ToolOutcome,
    TurnEnd,
)

Behavior = Callable[["FakeSession", str], Coroutine[Any, Any, None]]

_ITEM = re.compile(r"^\d+: thread (\w+), about ")


class PromptItem(NamedTuple):
    thread_id: str
    text: str
    follow_up: bool


def items_of(prompt: str) -> list[PromptItem]:
    """The items of a batch prompt, in prompt order."""
    items: list[PromptItem] = []
    for chunk in prompt.split("\n\nItem ")[1:]:
        match = _ITEM.match(chunk)
        assert match, chunk
        follow_up = "\nFollow-up:\n" in chunk
        text = chunk.split("\nFollow-up:\n" if follow_up else "\nQuestion:\n", 1)[1]
        items.append(PromptItem(match.group(1), text.strip(), follow_up))
    return items


class FakeSession:
    """An AgentSession that models one CLI process: a single buffered event stream that
    every turn writes to, so a turn left undrained leaks into the next one."""

    def __init__(self, spec: SessionSpec, owner: "FakeAgents") -> None:
        self.spec = spec
        self.owner = owner
        self.stream: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        self.prompts: list[str] = []
        self.cost = 0.0
        self.closed = False
        self.interrupts = 0
        self.interrupted = asyncio.Event()
        self.turn: asyncio.Task[None] | None = None
        self.tool_results: list[ToolOutcome] = []
        self._blocks = 0

    async def send(self, prompt: str) -> None:
        self.prompts.append(prompt)
        self.interrupted = asyncio.Event()
        self.turn = asyncio.create_task(self.owner.behavior(self, prompt))

    async def events(self) -> AsyncIterator[AgentEvent]:
        while True:
            event = await self.stream.get()
            if event is None:
                return
            yield event
            if isinstance(event, TurnEnd):
                return

    async def interrupt(self) -> None:
        self.interrupts += 1
        self.interrupted.set()

    async def close(self) -> None:
        self.closed = True
        if self.turn is not None and not self.turn.done():
            self.turn.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.turn
        await self.stream.put(None)

    async def context_tokens(self) -> int | None:
        return 1000 + 10 * len(self.prompts)

    async def say(self, *chunks: str) -> None:
        for chunk in chunks:
            await self.stream.put(TextDelta(chunk))

    async def answer(self, thread_id: str, answer: str, *, chunk_size: int = 7) -> ToolOutcome:
        """Stream the answer_question input in chunks, then run the tool, like the CLI."""
        self._blocks += 1
        raw = json.dumps({"thread_id": thread_id, "answer": answer})
        for start in range(0, len(raw), chunk_size):
            await self.stream.put(
                ToolInputDelta(ANSWER_TOOL, f"b{self._blocks}", raw[start : start + chunk_size])
            )
        await asyncio.sleep(0.01)
        outcome = await self.spec.answer({"thread_id": thread_id, "answer": answer})
        self.tool_results.append(outcome)
        return outcome

    async def end(
        self,
        text: str | None,
        *,
        cost: float = 0.01,
        is_error: bool = False,
        subtype: str = "success",
        aborted: bool = False,
    ) -> None:
        self.cost += cost
        await self.stream.put(
            TurnEnd(
                result_text=text,
                total_cost_usd=self.cost,
                session_id=self.spec.session_id,
                is_error=is_error,
                subtype=subtype,
                aborted=aborted,
            )
        )


async def answer_all(session: FakeSession, prompt: str) -> None:
    """Answer every batch item with answer_question; a warm-up prompt gets a text answer."""
    items = items_of(prompt)
    if not items:
        await session.say("answer to ", "warm-up")
        await session.end("answer to warm-up")
        return
    for item in items:
        await session.answer(item.thread_id, f"answer to {item.text}")
    await session.end("done")


async def long_answer(session: FakeSession, prompt: str) -> None:
    """Answer the first item, then stream text until interrupted, then end aborted."""
    items = items_of(prompt)
    if items:
        await session.answer(items[0].thread_id, f"answer to {items[0].text}")
    while not session.interrupted.is_set():
        await session.say("word ")
        await asyncio.sleep(0.01)
    await session.say("tail-after-interrupt ")
    await session.end(None, is_error=True, subtype="error_during_execution", aborted=True)


@dataclass
class FakeAgents:
    behavior: Behavior = answer_all
    sessions: list[FakeSession] = field(default_factory=list)

    async def factory(self, spec: SessionSpec) -> FakeSession:
        session = FakeSession(spec, self)
        self.sessions.append(session)
        return session

    def for_review(self, review_id: str) -> list[FakeSession]:
        return [s for s in self.sessions if s.spec.review_id == review_id]
