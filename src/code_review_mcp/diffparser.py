"""Parse unified diffs to extract file paths and changed line ranges."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class LineChange:
    line_number: int  # line number in the new file
    change_type: str  # "add", "delete", or "context"
    content: str


@dataclass
class FileDiff:
    old_path: str
    new_path: str
    changes: list[LineChange] = field(default_factory=list)
    # Sets of line numbers that changed (in the new file for adds, old file for deletes)
    added_lines: set[int] = field(default_factory=set)
    deleted_lines: set[int] = field(default_factory=set)


_DIFF_HEADER = re.compile(r"^diff --git a/(.*) b/(.*)")
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_diff(diff_text: str) -> list[FileDiff]:
    """Parse a unified diff into structured file diffs."""
    files: list[FileDiff] = []
    current: FileDiff | None = None
    new_line = 0

    for line in diff_text.split("\n"):
        # New file header
        m = _DIFF_HEADER.match(line)
        if m:
            current = FileDiff(old_path=m.group(1), new_path=m.group(2))
            files.append(current)
            continue

        # Skip --- and +++ lines
        if line.startswith("--- ") or line.startswith("+++ "):
            continue

        # Hunk header
        m = _HUNK_HEADER.match(line)
        if m and current is not None:
            new_line = int(m.group(2))
            continue

        if current is None:
            continue

        # Diff lines
        if line.startswith("+"):
            current.added_lines.add(new_line)
            current.changes.append(LineChange(new_line, "add", line[1:]))
            new_line += 1
        elif line.startswith("-"):
            current.deleted_lines.add(new_line)
            current.changes.append(LineChange(new_line, "delete", line[1:]))
            # Deleted lines don't advance new_line
        elif line.startswith(" "):
            current.changes.append(LineChange(new_line, "context", line[1:]))
            new_line += 1

    return files
