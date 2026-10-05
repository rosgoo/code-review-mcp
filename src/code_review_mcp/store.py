import json
import sqlite3
import uuid
from collections.abc import Collection, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

ReviewKind = Literal["pr", "local"]
ReviewStatus = Literal["open", "submitted", "closed"]
ReviewMode = Literal["diff", "files", "empty"]
ThreadKind = Literal["local", "question", "review_comment"]
ThreadStatus = Literal["draft", "submitted", "resolved", "stale", "posted"]
Side = Literal["additions", "deletions"]
Author = Literal["user", "agent"]
SubmissionEvent = Literal["APPROVE", "COMMENT", "REQUEST_CHANGES"]

MIGRATIONS: list[str] = [
    """
    CREATE TABLE reviews (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('pr', 'local')),
        title TEXT NOT NULL,
        repo TEXT,
        pr_number INTEGER,
        author TEXT,
        url TEXT,
        base_sha TEXT,
        merge_base_sha TEXT,
        head_sha TEXT,
        last_reviewed_sha TEXT,
        worktree_path TEXT,
        agent_session_id TEXT,
        agent_cost_usd REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'submitted', 'closed')),
        patch_text TEXT,
        working_dir TEXT,
        mode TEXT CHECK (mode IN ('diff', 'files', 'empty')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX reviews_created_at ON reviews (created_at);

    CREATE TABLE review_files (
        review_id TEXT NOT NULL REFERENCES reviews (id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        path TEXT NOT NULL,
        content TEXT NOT NULL,
        language TEXT NOT NULL,
        added_lines TEXT NOT NULL DEFAULT '[]',
        deleted_lines TEXT NOT NULL DEFAULT '[]',
        deleted_content TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (review_id, position)
    );

    CREATE TABLE threads (
        id TEXT PRIMARY KEY,
        review_id TEXT NOT NULL REFERENCES reviews (id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('local', 'question', 'review_comment')),
        path TEXT NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('additions', 'deletions')),
        line INTEGER NOT NULL,
        start_line INTEGER,
        start_side TEXT CHECK (start_side IN ('additions', 'deletions')),
        line_content TEXT NOT NULL DEFAULT '',
        anchor_sha TEXT,
        status TEXT NOT NULL
            CHECK (status IN ('draft', 'submitted', 'resolved', 'stale', 'posted')),
        created_by TEXT NOT NULL CHECK (created_by IN ('user', 'agent')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX threads_review ON threads (review_id, created_at);

    CREATE TABLE messages (
        id TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL REFERENCES threads (id) ON DELETE CASCADE,
        author TEXT NOT NULL CHECK (author IN ('user', 'agent')),
        body TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE INDEX messages_thread ON messages (thread_id, created_at);

    CREATE TABLE submissions (
        id TEXT PRIMARY KEY,
        review_id TEXT NOT NULL REFERENCES reviews (id) ON DELETE CASCADE,
        event TEXT NOT NULL CHECK (event IN ('APPROVE', 'COMMENT', 'REQUEST_CHANGES')),
        body TEXT NOT NULL DEFAULT '',
        github_review_id INTEGER,
        submitted_at TEXT NOT NULL
    );
    CREATE INDEX submissions_review ON submissions (review_id, submitted_at);
    """,
]


class SchemaVersionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReviewRow:
    id: str
    kind: ReviewKind
    title: str
    repo: str | None
    pr_number: int | None
    author: str | None
    url: str | None
    base_sha: str | None
    merge_base_sha: str | None
    head_sha: str | None
    last_reviewed_sha: str | None
    worktree_path: str | None
    agent_session_id: str | None
    agent_cost_usd: float
    status: ReviewStatus
    patch_text: str | None
    working_dir: str | None
    mode: ReviewMode | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ReviewFile:
    path: str
    content: str
    language: str
    added_lines: list[int] = field(default_factory=list)
    deleted_lines: list[int] = field(default_factory=list)
    deleted_content: dict[int, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class ThreadRow:
    id: str
    review_id: str
    kind: ThreadKind
    path: str
    side: Side
    line: int
    start_line: int | None
    start_side: Side | None
    line_content: str
    anchor_sha: str | None
    status: ThreadStatus
    created_by: Author
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class MessageRow:
    id: str
    thread_id: str
    author: Author
    body: str
    created_at: str


@dataclass(frozen=True)
class SubmissionRow:
    id: str
    review_id: str
    event: SubmissionEvent
    body: str
    github_review_id: int | None
    submitted_at: str


def new_id() -> str:
    return uuid.uuid4().hex


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    """SQLite-backed review state. One connection, used from one thread."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @classmethod
    def open(cls, path: Path) -> Self:
        """Open (creating if needed) the database at `path` and apply pending migrations.

        Raises SchemaVersionError if the database was written by a newer schema.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, autocommit=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        store = cls(conn)
        try:
            store._migrate()
        except (sqlite3.Error, SchemaVersionError):
            conn.close()
            raise
        return store

    def close(self) -> None:
        self._conn.close()

    @property
    def schema_version(self) -> int:
        version: int = self._conn.execute("PRAGMA user_version").fetchone()[0]
        return version

    @property
    def journal_mode(self) -> str:
        mode: str = self._conn.execute("PRAGMA journal_mode").fetchone()[0]
        return mode

    def _migrate(self) -> None:
        current = self.schema_version
        if current > len(MIGRATIONS):
            raise SchemaVersionError(
                f"Database schema version {current} is newer than this code "
                f"(knows {len(MIGRATIONS)})"
            )
        for target, script in enumerate(MIGRATIONS[current:], start=current + 1):
            try:
                self._conn.executescript(
                    f"BEGIN;\n{script}\nPRAGMA user_version = {target};\nCOMMIT;"
                )
            except sqlite3.Error:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def create_review(
        self,
        *,
        kind: ReviewKind,
        title: str,
        mode: ReviewMode | None = None,
        patch_text: str | None = None,
        working_dir: str | None = None,
        files: Sequence[ReviewFile] = (),
    ) -> ReviewRow:
        review_id = new_id()
        now = utc_now()
        with self._transaction():
            self._conn.execute(
                "INSERT INTO reviews"
                " (id, kind, title, mode, patch_text, working_dir, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (review_id, kind, title, mode, patch_text, working_dir, now, now),
            )
            self._insert_files(review_id, files)
        review = self.get_review(review_id)
        assert review is not None
        return review

    def get_review(self, review_id: str) -> ReviewRow | None:
        row = self._conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
        return None if row is None else ReviewRow(**dict(row))

    def list_reviews(self, limit: int = 50) -> list[ReviewRow]:
        rows = self._conn.execute(
            "SELECT * FROM reviews ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [ReviewRow(**dict(row)) for row in rows]

    def update_review_content(
        self,
        review_id: str,
        *,
        mode: ReviewMode,
        patch_text: str | None,
        files: Sequence[ReviewFile],
    ) -> None:
        with self._transaction():
            self._conn.execute(
                "UPDATE reviews SET mode = ?, patch_text = ?, updated_at = ? WHERE id = ?",
                (mode, patch_text, utc_now(), review_id),
            )
            self._conn.execute("DELETE FROM review_files WHERE review_id = ?", (review_id,))
            self._insert_files(review_id, files)

    def touch_review(self, review_id: str) -> None:
        self._conn.execute("UPDATE reviews SET updated_at = ? WHERE id = ?", (utc_now(), review_id))

    def _insert_files(self, review_id: str, files: Sequence[ReviewFile]) -> None:
        self._conn.executemany(
            "INSERT INTO review_files"
            " (review_id, position, path, content, language,"
            "  added_lines, deleted_lines, deleted_content)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    review_id,
                    position,
                    f.path,
                    f.content,
                    f.language,
                    json.dumps(f.added_lines),
                    json.dumps(f.deleted_lines),
                    json.dumps({str(k): v for k, v in f.deleted_content.items()}),
                )
                for position, f in enumerate(files)
            ],
        )

    def list_review_files(self, review_id: str) -> list[ReviewFile]:
        rows = self._conn.execute(
            "SELECT * FROM review_files WHERE review_id = ? ORDER BY position", (review_id,)
        ).fetchall()
        return [
            ReviewFile(
                path=row["path"],
                content=row["content"],
                language=row["language"],
                added_lines=json.loads(row["added_lines"]),
                deleted_lines=json.loads(row["deleted_lines"]),
                deleted_content={int(k): v for k, v in json.loads(row["deleted_content"]).items()},
            )
            for row in rows
        ]

    def create_thread(
        self,
        *,
        review_id: str,
        kind: ThreadKind,
        path: str,
        side: Side,
        line: int,
        status: ThreadStatus,
        author: Author,
        body: str,
        line_content: str = "",
        start_line: int | None = None,
        start_side: Side | None = None,
        anchor_sha: str | None = None,
    ) -> ThreadRow:
        """Create a thread and its first message, both written by `author`."""
        thread_id = new_id()
        now = utc_now()
        with self._transaction():
            self._conn.execute(
                "INSERT INTO threads"
                " (id, review_id, kind, path, side, line, start_line, start_side,"
                "  line_content, anchor_sha, status, created_by, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    thread_id,
                    review_id,
                    kind,
                    path,
                    side,
                    line,
                    start_line,
                    start_side,
                    line_content,
                    anchor_sha,
                    status,
                    author,
                    now,
                    now,
                ),
            )
            self._conn.execute(
                "INSERT INTO messages (id, thread_id, author, body, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (new_id(), thread_id, author, body, now),
            )
        thread = self.get_thread(thread_id)
        assert thread is not None
        return thread

    def get_thread(self, thread_id: str) -> ThreadRow | None:
        row = self._conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        return None if row is None else ThreadRow(**dict(row))

    def list_threads(
        self,
        review_id: str,
        *,
        kind: ThreadKind | None = None,
        statuses: Collection[ThreadStatus] | None = None,
    ) -> list[ThreadRow]:
        sql = "SELECT * FROM threads WHERE review_id = ?"
        params: list[object] = [review_id]
        if kind is not None:
            sql += " AND kind = ?"
            params.append(kind)
        if statuses is not None:
            sql += f" AND status IN ({', '.join('?' * len(statuses))})"
            params.extend(statuses)
        sql += " ORDER BY created_at, rowid"
        return [ThreadRow(**dict(row)) for row in self._conn.execute(sql, params).fetchall()]

    def set_thread_status(self, thread_id: str, status: ThreadStatus) -> None:
        self._conn.execute(
            "UPDATE threads SET status = ?, updated_at = ? WHERE id = ?",
            (status, utc_now(), thread_id),
        )

    def move_threads(
        self,
        review_id: str,
        *,
        kind: ThreadKind,
        from_status: ThreadStatus,
        to_status: ThreadStatus,
    ) -> int:
        """Move every `kind` thread of the review from `from_status` to `to_status`.

        Returns the number of threads moved.
        """
        cursor = self._conn.execute(
            "UPDATE threads SET status = ?, updated_at = ?"
            " WHERE review_id = ? AND kind = ? AND status = ?",
            (to_status, utc_now(), review_id, kind, from_status),
        )
        return cursor.rowcount

    def add_message(self, thread_id: str, *, author: Author, body: str) -> MessageRow:
        message = MessageRow(
            id=new_id(), thread_id=thread_id, author=author, body=body, created_at=utc_now()
        )
        with self._transaction():
            self._conn.execute(
                "INSERT INTO messages (id, thread_id, author, body, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (message.id, thread_id, author, body, message.created_at),
            )
            self._conn.execute(
                "UPDATE threads SET updated_at = ? WHERE id = ?", (message.created_at, thread_id)
            )
        return message

    def list_messages(self, thread_id: str) -> list[MessageRow]:
        rows = self._conn.execute(
            "SELECT * FROM messages WHERE thread_id = ? ORDER BY created_at, rowid", (thread_id,)
        ).fetchall()
        return [MessageRow(**dict(row)) for row in rows]

    def messages_for_review(self, review_id: str) -> dict[str, list[MessageRow]]:
        """Return every message of the review's threads, grouped by thread id, oldest first."""
        rows = self._conn.execute(
            "SELECT m.* FROM messages m JOIN threads t ON t.id = m.thread_id"
            " WHERE t.review_id = ? ORDER BY m.created_at, m.rowid",
            (review_id,),
        ).fetchall()
        grouped: dict[str, list[MessageRow]] = {}
        for row in rows:
            message = MessageRow(**dict(row))
            grouped.setdefault(message.thread_id, []).append(message)
        return grouped

    def add_submission(
        self,
        review_id: str,
        *,
        event: SubmissionEvent,
        body: str,
        github_review_id: int | None = None,
    ) -> SubmissionRow:
        submission = SubmissionRow(
            id=new_id(),
            review_id=review_id,
            event=event,
            body=body,
            github_review_id=github_review_id,
            submitted_at=utc_now(),
        )
        self._conn.execute(
            "INSERT INTO submissions (id, review_id, event, body, github_review_id, submitted_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                submission.id,
                review_id,
                event,
                body,
                github_review_id,
                submission.submitted_at,
            ),
        )
        return submission

    def list_submissions(self, review_id: str) -> list[SubmissionRow]:
        rows = self._conn.execute(
            "SELECT * FROM submissions WHERE review_id = ? ORDER BY submitted_at, rowid",
            (review_id,),
        ).fetchall()
        return [SubmissionRow(**dict(row)) for row in rows]
