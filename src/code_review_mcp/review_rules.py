from collections.abc import Sequence
from dataclasses import dataclass

from code_review_mcp.errors import ReviewError
from code_review_mcp.store import Side, SubmissionEvent
from code_review_mcp.worktrees import Hunk

ALL_EVENTS: tuple[SubmissionEvent, ...] = ("COMMENT", "APPROVE", "REQUEST_CHANGES")
AUTHOR_EVENTS: tuple[SubmissionEvent, ...] = ("COMMENT",)
BODY_REQUIRED_EVENTS: frozenset[SubmissionEvent] = frozenset({"COMMENT", "REQUEST_CHANGES"})
FILE_LEVEL_LINE = 0


@dataclass(frozen=True)
class Anchor:
    side: Side
    line: int
    start_line: int | None
    start_side: Side | None

    @property
    def is_file_level(self) -> bool:
        return self.line == FILE_LEVEL_LINE


def is_author(viewer_login: str, pr_author: str | None) -> bool:
    return pr_author is not None and viewer_login.lower() == pr_author.lower()


def allowed_events(viewer_login: str, pr_author: str | None) -> list[SubmissionEvent]:
    """GitHub lets a PR's author only COMMENT on their own PR; anyone else may also APPROVE or
    REQUEST_CHANGES."""
    return list(AUTHOR_EVENTS if is_author(viewer_login, pr_author) else ALL_EVENTS)


def check_review_body(event: SubmissionEvent, body: str) -> None:
    """GitHub requires a review body for COMMENT and REQUEST_CHANGES. Raises ReviewError."""
    if event in BODY_REQUIRED_EVENTS and not body.strip():
        raise ReviewError(f"A {event} review needs a body. Write a summary, then submit again.")


def normalize_anchor(
    side: Side | None, line: int, start_line: int | None, start_side: Side | None
) -> Anchor:
    """Validate a comment position and drop fields that do not apply.

    Line 0 is a file comment: side and start fields are ignored. A range whose start equals
    its end on the same side becomes a single-line comment. Raises ReviewError.
    """
    if line < 0:
        raise ReviewError(f"line must be 0 (a file comment) or a positive line, got {line}")
    if line == FILE_LEVEL_LINE:
        return Anchor(side="additions", line=FILE_LEVEL_LINE, start_line=None, start_side=None)
    if side is None:
        raise ReviewError("side is required for a line comment: additions or deletions")
    if start_line is None:
        return Anchor(side=side, line=line, start_line=None, start_side=None)
    if start_line < 1:
        raise ReviewError(f"start_line must be a positive line, got {start_line}")
    start_side = start_side or side
    if start_side == side and start_line == line:
        return Anchor(side=side, line=line, start_line=None, start_side=None)
    if start_side == side and start_line > line:
        raise ReviewError(f"start_line {start_line} must come before line {line}")
    return Anchor(side=side, line=line, start_line=start_line, start_side=start_side)


def _side_lines(hunk: Hunk, side: Side) -> range:
    return hunk.new_lines if side == "additions" else hunk.old_lines


def check_commentable(hunks: Sequence[Hunk], anchor: Anchor, path: str) -> None:
    """Raise ReviewError unless every line of the anchor is inside one diff hunk.

    GitHub accepts a line comment only on a line of the PR diff, and a range only within
    one hunk. A file comment is always accepted.
    """
    if anchor.is_file_level:
        return
    hunk = next((h for h in hunks if anchor.line in _side_lines(h, anchor.side)), None)
    if hunk is None:
        raise ReviewError(
            f"Line {anchor.line} on the {anchor.side} side of {path} is outside the diff. "
            "Comment on a line inside a diff hunk, or use line 0 for a file comment."
        )
    if anchor.start_line is None or anchor.start_side is None:
        return
    if anchor.start_line not in _side_lines(hunk, anchor.start_side):
        raise ReviewError(
            f"The range from line {anchor.start_line} ({anchor.start_side}) to line "
            f"{anchor.line} ({anchor.side}) of {path} is not inside one diff hunk. Every "
            "line of a range comment must be in the same hunk."
        )


def commentable_ranges(hunks: Sequence[Hunk]) -> dict[str, list[list[int]]]:
    """1-based inclusive line ranges, per side, that a line comment may target."""
    return {
        "additions": [
            [h.new_lines.start, h.new_lines.stop - 1] for h in hunks if len(h.new_lines) > 0
        ],
        "deletions": [
            [h.old_lines.start, h.old_lines.stop - 1] for h in hunks if len(h.old_lines) > 0
        ],
    }
