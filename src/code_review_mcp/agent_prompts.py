from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from code_review_mcp.store import MessageRow, ReviewRow, ThreadRow
from code_review_mcp.worktrees import ChangedFile

BODY_LIMIT = 4000
COMMENT_LIMIT = 600
EARLIER_LIMIT = 1500

WARMUP_QUESTION = (
    "Read the diff and the touched files. Summarize the change, the risks, and the "
    "questions you would ask the author."
)


def review_instructions(repo: str, number: int) -> str:
    """The text appended to the agent's system prompt for one PR review."""
    return (
        f"You help a reviewer understand pull request {repo}#{number}. The current directory "
        "is a git worktree checked out at the PR head. Each message tells you the head and "
        "merge base SHAs; the PR diff is `git diff <merge_base>..<head>`.\n\n"
        "Rules:\n"
        "- You are read-only. Never create, edit, or delete files. Use Read, Grep, and Glob "
        "inside the worktree, and only `git log`, `git show`, `git diff`, and `git blame` in "
        "Bash. Other commands are denied.\n"
        "- A deletions-side line is in the old file at the merge base; read it with "
        "`git show <merge_base>:<path>`.\n"
        "- Cite code as path:line.\n"
        "- Questions arrive in batches of numbered items. Answer every item by calling "
        "answer_question(thread_id, answer) once per item, with a markdown answer. You may "
        "read code first and relate the questions to each other. Text you write outside "
        "answer_question is not shown to the user.\n"
        "- Call draft_review_comment only when the user asks you to draft a review comment. "
        "It saves a draft that the user edits or deletes before anything is posted. Use a "
        "line inside the PR diff, or line 0 for a file comment.\n"
        "- Answer the question asked, briefly and directly. Say when you are unsure."
    )


def _short(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def location(thread: ThreadRow) -> str:
    """Where a thread points, in words: the PR, a file, a line, or a range."""
    if not thread.path:
        return "the whole PR"
    if thread.line == 0:
        return f"the file {thread.path}"
    side = "new file, additions side" if thread.side == "additions" else "old file, deletions side"
    if thread.start_line is not None:
        start_side = "" if thread.start_side in (None, thread.side) else f" ({thread.start_side})"
        return f"{thread.path} lines {thread.start_line}{start_side} to {thread.line} ({side})"
    return f"{thread.path}:{thread.line} ({side})"


def pr_context(review: ReviewRow, files: Sequence[ChangedFile]) -> str:
    """The PR facts a new session needs: title, refs, SHAs, description, and changed files."""
    lines = [
        f"Pull request {review.repo}#{review.pr_number}: {review.title}",
        f"Base {review.base_ref} <- head {review.head_ref}. "
        f"Merge base {review.merge_base_sha}, head {review.head_sha}.",
    ]
    if review.pr_body and review.pr_body.strip():
        lines += ["Description:", _short(review.pr_body, BODY_LIMIT)]
    if files:
        lines.append("Changed files:")
        for f in files:
            renamed = f" (from {f.old_path})" if f.old_path else ""
            lines.append(f"- {f.status} {f.path}{renamed}")
    return "\n".join(lines)


def _first_body(thread: ThreadRow, messages: Mapping[str, Sequence[MessageRow]]) -> str:
    found = messages.get(thread.id)
    return _short(found[0].body, COMMENT_LIMIT) if found else ""


def review_delta(
    review: ReviewRow,
    threads: Sequence[ThreadRow],
    messages: Mapping[str, Sequence[MessageRow]],
    viewed: Sequence[str],
    *,
    exclude_thread_ids: Collection[str],
    head_changes: Sequence[ChangedFile] | None,
) -> str:
    """What changed in the review since the agent's last turn, or "" when nothing did.

    Covers review comments (new, edited, posted, stale), new and resolved threads, files
    marked viewed, and a moved head. With no earlier turn, every existing review comment
    and thread counts as new. Staged questions, which the user has not sent, never appear.
    """
    since = review.agent_last_turn_at
    items: list[str] = []
    if (
        review.agent_head_sha is not None
        and review.head_sha is not None
        and review.agent_head_sha != review.head_sha
    ):
        moved = f"The PR head moved from {review.agent_head_sha[:12]} to {review.head_sha[:12]}."
        if head_changes is None:
            moved += " The files changed between them could not be listed."
        elif head_changes:
            moved += " Files changed between them: " + ", ".join(
                f"{f.status} {f.path}" for f in head_changes
            )
        items.append(moved)
    for thread in threads:
        if thread.id in exclude_thread_ids or (
            thread.kind == "question" and thread.status == "draft"
        ):
            continue
        created = since is None or thread.created_at > since
        updated = since is None or thread.updated_at > since
        body = _first_body(thread, messages)
        if thread.kind == "review_comment":
            if created:
                label = f"New {thread.status} review comment by the {thread.created_by}"
            elif not updated:
                continue
            elif thread.status == "posted":
                label = "Review comment posted to GitHub"
            elif thread.status == "stale":
                label = "Review comment now stale (its line changed)"
            else:
                label = f"Edited {thread.status} review comment"
            items.append(f'{label} on {location(thread)}: "{body}"')
        elif thread.status == "resolved" and updated:
            items.append(f'Resolved thread on {location(thread)}: "{body}"')
        elif created and thread.created_by == "user":
            items.append(f'New question thread on {location(thread)}: "{body}"')
    if viewed:
        items.append("Files marked viewed: " + ", ".join(viewed))
    if not items:
        return ""
    return "Changes in the review since your last turn:\n" + "\n".join(f"- {i}" for i in items)


@dataclass(frozen=True)
class BatchItem:
    thread: ThreadRow
    earlier: Sequence[MessageRow]
    sent: Sequence[MessageRow]

    @property
    def follow_up(self) -> bool:
        return bool(self.earlier)


def item_location(thread: ThreadRow) -> str:
    """`path:line[-end]` and side, "file <path>", or "the PR"."""
    if not thread.path:
        return "the PR"
    if thread.line == 0:
        return f"file {thread.path}"
    if thread.start_line is not None:
        sides = (
            f"{thread.start_side} to {thread.side} side"
            if thread.start_side not in (None, thread.side)
            else f"{thread.side} side"
        )
        return f"{thread.path}:{thread.start_line}-{thread.line} ({sides})"
    return f"{thread.path}:{thread.line} ({thread.side} side)"


def batch_prompt(
    review: ReviewRow, items: Sequence[BatchItem], *, context: str | None, delta: str
) -> str:
    """The prompt for one batch turn: every item with its thread id, location, and text."""
    parts: list[str] = []
    if context:
        parts.append(context)
    if delta:
        parts.append(delta)
    parts.append(
        f"Answer {len(items)} item(s) about {review.repo}#{review.pr_number}. Head "
        f"{review.head_sha}, merge base {review.merge_base_sha}. An additions-side line is in "
        "the new file at the head; a deletions-side line is in the old file at the merge "
        "base. Answer every item by calling answer_question(thread_id, answer) once per "
        "item. You may read code first and relate the items to each other."
    )
    for number, item in enumerate(items, start=1):
        lines = [f"Item {number}: thread {item.thread.id}, about {item_location(item.thread)}"]
        if item.earlier:
            lines.append("Earlier messages in this thread:")
            lines += [f"[{m.author}] {_short(m.body, EARLIER_LIMIT)}" for m in item.earlier]
        label = "Follow-up" if item.follow_up else "Question"
        lines.append(f"{label}:\n" + "\n\n".join(m.body.strip() for m in item.sent))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def warmup_prompt(review: ReviewRow, context: str, delta: str) -> str:
    """The prompt for the warm-up turn, which writes the review's overview."""
    parts = [context]
    if delta:
        parts.append(delta)
    parts.append(
        f"{WARMUP_QUESTION} The diff is `git diff {review.merge_base_sha}..{review.head_sha}`."
    )
    return "\n\n".join(parts)
