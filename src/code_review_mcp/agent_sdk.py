import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    HookMatcher,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    create_sdk_mcp_server,
    tool,
)

from code_review_mcp.agent_gates import ANSWER_TOOL, DRAFT_TOOL, make_pre_tool_use_gate
from code_review_mcp.agents import (
    AgentEvent,
    AgentFailure,
    SessionSpec,
    TextDelta,
    ToolHandler,
    ToolInputDelta,
    TurnEnd,
)

logger = logging.getLogger(__name__)

TOOLS = ["Read", "Grep", "Glob", "Bash"]
ALLOWED_TOOLS = [
    "Read",
    "Grep",
    "Glob",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(git diff:*)",
    "Bash(git blame:*)",
    DRAFT_TOOL,
    ANSWER_TOOL,
]
DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "File path in the PR, relative to the repo"},
        "line": {
            "type": "integer",
            "description": "Line number on `side`; 0 for a comment on the whole file",
        },
        "side": {
            "type": "string",
            "enum": ["additions", "deletions"],
            "description": "additions = new file at the head; deletions = old file",
        },
        "start_line": {
            "type": "integer",
            "description": "First line of a multi-line comment on the same side",
        },
        "body": {"type": "string", "description": "The comment text, in markdown"},
    },
    "required": ["path", "line", "side", "body"],
}


ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thread_id": {"type": "string", "description": "The item's thread id"},
        "answer": {"type": "string", "description": "The answer to that item, in markdown"},
    },
    "required": ["thread_id", "answer"],
}


def _tool_result(text: str, ok: bool) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if not ok:
        result["is_error"] = True
    return result


def review_server(draft: ToolHandler, answer: ToolHandler) -> Any:
    """An in-process MCP server named "review" with draft_review_comment and
    answer_question."""

    @tool(
        "draft_review_comment",
        "Save a draft review comment on a line inside the PR diff (or line 0 for the whole "
        "file). The user edits or deletes drafts before anything is posted to GitHub. Use "
        "it only when the user asks you to draft a comment.",
        DRAFT_SCHEMA,
    )
    async def draft_review_comment(args: dict[str, Any]) -> dict[str, Any]:
        outcome = await draft(args)
        return _tool_result(outcome.text, outcome.ok)

    @tool(
        "answer_question",
        "Answer one item of the current batch. Call it once per item, with the item's "
        "thread_id and a markdown answer. The user sees only answers given this way.",
        ANSWER_SCHEMA,
    )
    async def answer_question(args: dict[str, Any]) -> dict[str, Any]:
        outcome = await answer(args)
        return _tool_result(outcome.text, outcome.ok)

    return create_sdk_mcp_server(
        name="review", version="1.0.0", tools=[draft_review_comment, answer_question]
    )


def session_options(spec: SessionSpec) -> ClaudeAgentOptions:
    """The tested option set: project CLAUDE.md, no repo hooks or MCP servers, read-only
    tools behind the PreToolUse gate, and one in-process draft tool."""
    return ClaudeAgentOptions(
        cwd=str(spec.cwd),
        model=spec.model,
        setting_sources=["project"],
        system_prompt={"type": "preset", "preset": "claude_code", "append": spec.instructions},
        permission_mode="dontAsk",
        tools=TOOLS,
        allowed_tools=ALLOWED_TOOLS,
        settings=json.dumps({"disableAllHooks": True}),
        strict_mcp_config=True,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[make_pre_tool_use_gate(spec.cwd)])]},
        mcp_servers={"review": review_server(spec.draft, spec.answer)},
        include_partial_messages=True,
        max_turns=spec.max_turns,
        max_budget_usd=spec.max_budget_usd,
        resume=spec.session_id if spec.resume else None,
        session_id=None if spec.resume else spec.session_id,
    )


class SdkAgentSession:
    """An AgentSession backed by one ClaudeSDKClient (one CLI process)."""

    def __init__(self, client: ClaudeSDKClient) -> None:
        self._client = client
        self._model_logged = False

    async def send(self, prompt: str) -> None:
        try:
            await self._client.query(prompt)
        except ClaudeSDKError as e:
            raise AgentFailure(f"The agent client rejected the question: {e}") from e

    async def events(self) -> AsyncIterator[AgentEvent]:
        text_blocks: list[str] = []
        tools: dict[str, str] = {}
        message_number = 0
        try:
            async for message in self._client.receive_response():
                if isinstance(message, StreamEvent):
                    event = message.event
                    kind = event.get("type")
                    delta = event.get("delta") or {}
                    block = f"{message_number}:{event.get('index')}"
                    if kind == "message_start":
                        message_number += 1
                    elif kind == "content_block_start":
                        content = event.get("content_block") or {}
                        if content.get("type") == "tool_use":
                            tools[block] = str(content.get("name", ""))
                    elif kind == "content_block_delta" and delta.get("type") == "text_delta":
                        yield TextDelta(text=str(delta.get("text", "")))
                    elif kind == "content_block_delta" and delta.get("type") == (
                        "input_json_delta"
                    ):
                        yield ToolInputDelta(
                            tool=tools.get(block, ""),
                            block=block,
                            partial_json=str(delta.get("partial_json", "")),
                        )
                elif (
                    isinstance(message, SystemMessage)
                    and message.subtype == "init"
                    and not self._model_logged
                ):
                    self._model_logged = True
                    logger.info(
                        "agent session %s runs model %s",
                        message.data.get("session_id"),
                        message.data.get("model"),
                    )
                elif isinstance(message, AssistantMessage):
                    text_blocks += [b.text for b in message.content if isinstance(b, TextBlock)]
                elif isinstance(message, ResultMessage):
                    yield TurnEnd(
                        result_text=message.result,
                        total_cost_usd=message.total_cost_usd,
                        session_id=message.session_id,
                        is_error=message.is_error,
                        subtype=message.subtype,
                        aborted=message.terminal_reason == "aborted_streaming",
                    )
                    return
        except ClaudeSDKError as e:
            raise AgentFailure(f"The agent client failed: {e}") from e

    async def interrupt(self) -> None:
        try:
            await self._client.interrupt()
        except ClaudeSDKError as e:
            logger.warning("interrupting the agent client failed: %s", e)

    async def close(self) -> None:
        await self._client.disconnect()

    async def context_tokens(self) -> int | None:
        usage = await self._client.get_context_usage()
        return int(usage["totalTokens"])


async def open_sdk_session(spec: SessionSpec) -> SdkAgentSession:
    """Start a CLI process for the review and connect to it. Raises AgentFailure."""
    client = ClaudeSDKClient(session_options(spec))
    try:
        await client.connect()
    except ClaudeSDKError as e:
        raise AgentFailure(f"The agent client did not start: {e}") from e
    logger.info(
        "agent client for review %s started (%s session %s)",
        spec.review_id,
        "resumed" if spec.resume else "new",
        spec.session_id,
    )
    return SdkAgentSession(client)
