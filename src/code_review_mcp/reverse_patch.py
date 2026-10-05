import hashlib
import re
from dataclasses import dataclass, field

_FILE_HEADER = re.compile(r"^diff --git a/(.*) b/(.*)")
_INDEX_LINE = re.compile(r"^index ([0-9a-f]+)\.\.([0-9a-f]+)")
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_BINARY_MARKERS = ("Binary files ", "GIT binary patch")


@dataclass
class _Hunk:
    old_count: int
    new_start: int
    new_count: int
    lines: list[str] = field(default_factory=list)
    old_seen: int = 0
    new_seen: int = 0

    @property
    def complete(self) -> bool:
        return self.old_seen == self.old_count and self.new_seen == self.new_count


@dataclass
class _Section:
    old_absent: bool = False
    new_absent: bool = False
    old_blob: str | None = None
    new_blob: str | None = None
    hunks: list[_Hunk] = field(default_factory=list)


def split_file_patches(diff_text: str) -> dict[str, str]:
    """Map each new-side path of a git diff to the text of that file's section."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in diff_text.split("\n"):
        match = _FILE_HEADER.match(line)
        if match:
            current = sections[match.group(2)] = []
        if current is not None:
            current.append(line)
    return {path: "\n".join(lines) for path, lines in sections.items()}


def _parse_section(file_patch: str) -> _Section | None:
    """Parse one file's diff section, or return None for a binary or malformed one."""
    section = _Section()
    current: _Hunk | None = None
    for line in file_patch.split("\n"):
        match = _HUNK_HEADER.match(line)
        if match:
            if current is not None and not current.complete:
                return None
            old_count, new_start, new_count = match.group(2, 3, 4)
            current = _Hunk(
                old_count=1 if old_count is None else int(old_count),
                new_start=int(new_start),
                new_count=1 if new_count is None else int(new_count),
            )
            section.hunks.append(current)
        elif current is None:
            if line.startswith(_BINARY_MARKERS):
                return None
            section.old_absent = section.old_absent or line == "--- /dev/null"
            section.new_absent = section.new_absent or line == "+++ /dev/null"
            index = _INDEX_LINE.match(line)
            if index:
                section.old_blob, section.new_blob = index.groups()
        elif line.startswith("\\"):
            current.lines.append(line)
        elif not current.complete and line[:1] in (" ", "-", "+"):
            current.lines.append(line)
            current.old_seen += line[0] != "+"
            current.new_seen += line[0] != "-"
    if current is not None and not current.complete:
        return None
    return section


def _matches_blob(content: str, abbreviated_id: str | None) -> bool:
    """Check `content` against an abbreviated git blob id; an all-zero id means no file."""
    if abbreviated_id is None:
        return True
    if not abbreviated_id.strip("0"):
        return content == ""
    data = content.encode("utf-8")
    blob = b"blob %d\0" % len(data) + data
    return any(
        hashlib.new(algorithm, blob).hexdigest().startswith(abbreviated_id)
        for algorithm in ("sha1", "sha256")
    )


def _split_lines(content: str) -> tuple[list[str], bool]:
    if not content:
        return [], False
    lines = content.split("\n")
    if lines[-1] == "":
        return lines[:-1], True
    return lines, False


def reconstruct_old_content(new_content: str, file_patch: str) -> str | None:
    """Rebuild the old side of a file by reversing `file_patch` onto `new_content`.

    `file_patch` is one file's section of a unified git diff. Returns None when
    `new_content` does not match the new side of the hunks, when the section's
    `index` blob ids do not match the new or rebuilt old content, or when the
    section is binary or malformed. A section with no hunks (a rename or mode
    change) returns `new_content` unchanged.
    """
    section = _parse_section(file_patch)
    if section is None or not _matches_blob(new_content, section.new_blob):
        return None
    if section.new_absent and new_content:
        return None
    new_lines, new_has_eol = _split_lines(new_content)
    old_lines: list[str] = []
    cursor = 0
    old_missing_eol = False
    new_missing_eol = False

    for hunk in section.hunks:
        start = hunk.new_start - 1 if hunk.new_count else hunk.new_start
        if start < cursor or start > len(new_lines):
            return None
        old_lines.extend(new_lines[cursor:start])
        position = start
        previous = ""
        for line in hunk.lines:
            op, text = line[0], line[1:]
            if op == "\\":
                old_missing_eol = old_missing_eol or previous in (" ", "-")
                new_missing_eol = new_missing_eol or previous in (" ", "+")
                continue
            if op != "-":
                if position >= len(new_lines) or new_lines[position] != text:
                    return None
                position += 1
            if op != "+":
                old_lines.append(text)
            previous = op
        cursor = position

    if cursor < len(new_lines):
        old_lines.extend(new_lines[cursor:])
        old_has_eol = new_has_eol
    else:
        if new_lines and new_has_eol == new_missing_eol:
            return None
        old_has_eol = not old_missing_eol

    if section.old_absent and old_lines:
        return None
    old_content = "\n".join(old_lines) + ("\n" if old_has_eol and old_lines else "")
    return old_content if _matches_blob(old_content, section.old_blob) else None
