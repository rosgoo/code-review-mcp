from collections.abc import Sequence
from pathlib import Path

from code_review_mcp.diffparser import parse_diff
from code_review_mcp.errors import InvalidPathError
from code_review_mcp.store import ReviewFile

_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".less": "less",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".mdx": "markdown",
    ".markdown": "markdown",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".sql": "sql",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".xml": "xml",
    ".svg": "xml",
    ".dockerfile": "dockerfile",
    ".tf": "hcl",
    ".graphql": "graphql",
    ".gql": "graphql",
    ".r": "r",
}


def detect_language(file_path: str) -> str:
    return _EXT_TO_LANG.get(Path(file_path).suffix.lower(), "plaintext")


def require_absolute(value: str, name: str) -> Path:
    """Return `value` with `~` expanded. Raises InvalidPathError if it is still relative."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise InvalidPathError(f"{name} must be an absolute path, got {value!r}")
    return path


def read_text_file(path: Path) -> str:
    """Read a UTF-8 text file (undecodable bytes replaced). Raises OSError."""
    return path.read_text(encoding="utf-8", errors="replace")


def read_files(paths: Sequence[str]) -> list[ReviewFile]:
    """Load each path from disk; a missing or unreadable path becomes a placeholder file."""
    files: list[ReviewFile] = []
    for p in paths:
        resolved = Path(p).expanduser().resolve()
        if not resolved.is_file():
            hint = "" if Path(p).expanduser().is_absolute() else " (pass an absolute path)"
            files.append(
                ReviewFile(
                    path=p, content=f"# File not found: {resolved}{hint}", language="plaintext"
                )
            )
            continue
        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = f"# Error reading {p}"
        files.append(ReviewFile(path=p, content=text, language=detect_language(p)))
    return files


def build_annotated_files(diff_text: str, working_dir: str) -> list[ReviewFile]:
    """Read each new-side file of the diff from `working_dir`, annotated with its changes.

    Files that no longer exist on disk are skipped. Returns an empty list if none exist.
    """
    base = Path(working_dir).expanduser().resolve()
    views: list[ReviewFile] = []

    for fd in parse_diff(diff_text):
        file_path = base / fd.new_path
        if not file_path.is_file():
            continue
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        deleted_content: dict[int, list[str]] = {}
        current_del_block: list[str] = []
        for change in fd.changes:
            if change.change_type == "delete":
                current_del_block.append(change.content)
            elif current_del_block:
                deleted_content[change.line_number] = current_del_block
                current_del_block = []

        if current_del_block and fd.changes:
            last_new = max(
                (c.line_number for c in fd.changes if c.change_type != "delete"),
                default=1,
            )
            deleted_content[last_new + 1] = current_del_block

        views.append(
            ReviewFile(
                path=fd.new_path,
                content=content,
                language=detect_language(fd.new_path),
                added_lines=sorted(fd.added_lines),
                deleted_lines=sorted(fd.deleted_lines),
                deleted_content=deleted_content,
            )
        )

    return views


def added_lines_by_path(diff_text: str | None) -> dict[str, set[int]]:
    """Map each new-side path in the diff to its added line numbers (new-file numbering)."""
    if not diff_text:
        return {}
    return {fd.new_path: fd.added_lines for fd in parse_diff(diff_text)}
