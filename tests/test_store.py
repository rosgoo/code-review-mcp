import sqlite3
from pathlib import Path

import pytest

from code_review_mcp import store as store_module
from code_review_mcp.config import Settings
from code_review_mcp.store import MIGRATIONS, ReviewFile, SchemaVersionError, Store, ThreadStatus

SUBMITTED: tuple[ThreadStatus, ...] = ("submitted",)

EXPECTED_COLUMNS = {
    "reviews": {
        "id",
        "kind",
        "title",
        "repo",
        "pr_number",
        "author",
        "url",
        "base_sha",
        "merge_base_sha",
        "head_sha",
        "last_reviewed_sha",
        "worktree_path",
        "agent_session_id",
        "agent_cost_usd",
        "status",
        "patch_text",
        "working_dir",
        "mode",
        "created_at",
        "updated_at",
    },
    "review_files": {
        "review_id",
        "position",
        "path",
        "content",
        "language",
        "added_lines",
        "deleted_lines",
        "deleted_content",
    },
    "threads": {
        "id",
        "review_id",
        "kind",
        "path",
        "side",
        "line",
        "start_line",
        "start_side",
        "line_content",
        "anchor_sha",
        "status",
        "created_by",
        "created_at",
        "updated_at",
    },
    "messages": {"id", "thread_id", "author", "body", "created_at"},
    "submissions": {"id", "review_id", "event", "body", "github_review_id", "submitted_at"},
}


def _columns(db_path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _user_version(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        version: int = conn.execute("PRAGMA user_version").fetchone()[0]
        return version
    finally:
        conn.close()


def test_open_creates_full_schema_in_wal_mode(store: Store, settings: Settings) -> None:
    assert store.schema_version == len(MIGRATIONS)
    assert store.journal_mode == "wal"
    for table, columns in EXPECTED_COLUMNS.items():
        assert _columns(settings.db_path, table) == columns


def test_reopen_keeps_data_and_does_not_rerun_migrations(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "state.db"
    first = Store.open(db_path)
    review = first.create_review(kind="local", title="Keep me", mode="diff", patch_text="x")
    first.close()

    second = Store.open(db_path)
    try:
        assert second.schema_version == len(MIGRATIONS)
        assert second.get_review(review.id) == review
    finally:
        second.close()


def test_pending_migration_is_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "state.db"
    Store.open(db_path).close()
    monkeypatch.setattr(
        store_module, "MIGRATIONS", [*MIGRATIONS, "ALTER TABLE reviews ADD COLUMN note TEXT;"]
    )

    upgraded = Store.open(db_path)
    upgraded.close()

    assert _user_version(db_path) == len(MIGRATIONS) + 1
    assert "note" in _columns(db_path, "reviews")


def test_failed_migration_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "state.db"
    Store.open(db_path).close()
    monkeypatch.setattr(
        store_module,
        "MIGRATIONS",
        [*MIGRATIONS, "CREATE TABLE extra (a TEXT); THIS IS NOT SQL;"],
    )

    with pytest.raises(sqlite3.OperationalError):
        Store.open(db_path)

    assert _user_version(db_path) == len(MIGRATIONS)
    assert _columns(db_path, "extra") == set()


def test_newer_schema_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 5}")
    conn.close()

    with pytest.raises(SchemaVersionError):
        Store.open(db_path)


def test_review_create_get_list_update(store: Store) -> None:
    files = [
        ReviewFile(
            path="app.py",
            content="a\nb\n",
            language="python",
            added_lines=[2],
            deleted_lines=[2],
            deleted_content={2: ["old b"]},
        ),
        ReviewFile(path="README.md", content="# hi", language="markdown"),
    ]
    older = store.create_review(
        kind="local",
        title="Older",
        mode="files",
        patch_text="diff",
        working_dir="/repo",
        files=files,
    )
    newer = store.create_review(kind="local", title="Newer", mode="diff", patch_text="diff 2")

    assert older.kind == "local"
    assert older.status == "open"
    assert older.agent_cost_usd == 0
    assert older.pr_number is None
    assert store.get_review(older.id) == older
    assert store.get_review("missing") is None
    assert [r.id for r in store.list_reviews()] == [newer.id, older.id]
    assert store.list_review_files(older.id) == files

    store.update_review_content(older.id, mode="diff", patch_text="diff 3", files=[])
    updated = store.get_review(older.id)
    assert updated is not None
    assert updated.mode == "diff"
    assert updated.patch_text == "diff 3"
    assert updated.updated_at >= older.updated_at
    assert store.list_review_files(older.id) == []


def test_threads_and_messages(store: Store) -> None:
    review = store.create_review(kind="local", title="T", mode="diff", patch_text="d")
    first = store.create_thread(
        review_id=review.id,
        kind="local",
        path="app.py",
        side="additions",
        line=2,
        line_content="x = 2",
        status="draft",
        author="user",
        body="why 2?",
    )
    second = store.create_thread(
        review_id=review.id,
        kind="local",
        path="app.py",
        side="deletions",
        line=2,
        status="draft",
        author="user",
        body="why delete?",
    )

    assert first.created_by == "user"
    assert first.start_line is None
    assert [m.body for m in store.list_messages(first.id)] == ["why 2?"]

    reply = store.add_message(first.id, author="agent", body="because")
    assert [m.id for m in store.list_messages(first.id)][-1] == reply.id
    grouped = store.messages_for_review(review.id)
    assert [m.body for m in grouped[first.id]] == ["why 2?", "because"]
    assert [m.body for m in grouped[second.id]] == ["why delete?"]

    assert (
        store.move_threads(review.id, kind="local", from_status="draft", to_status="submitted") == 2
    )
    store.set_thread_status(second.id, "resolved")

    assert [t.id for t in store.list_threads(review.id)] == [first.id, second.id]
    assert [t.id for t in store.list_threads(review.id, statuses=SUBMITTED)] == [first.id]
    assert [t.id for t in store.list_threads(review.id, kind="question")] == []
    resolved = store.get_thread(second.id)
    assert resolved is not None
    assert resolved.status == "resolved"
    assert store.get_thread("missing") is None


def test_create_thread_is_atomic_and_enforces_foreign_keys(store: Store) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.create_thread(
            review_id="no-such-review",
            kind="local",
            path="a.py",
            side="additions",
            line=1,
            status="draft",
            author="user",
            body="orphan",
        )
    review = store.create_review(kind="local", title="T", mode="diff", patch_text="d")
    assert store.list_threads(review.id) == []
    assert store.messages_for_review(review.id) == {}


def test_check_constraints_reject_unknown_values(store: Store) -> None:
    review = store.create_review(kind="local", title="T", mode="diff", patch_text="d")
    with pytest.raises(sqlite3.IntegrityError):
        store.create_thread(
            review_id=review.id,
            kind="local",
            path="a.py",
            side="left",  # type: ignore[arg-type]
            line=1,
            status="draft",
            author="user",
            body="bad side",
        )


def test_submissions(store: Store) -> None:
    review = store.create_review(kind="pr", title="PR")
    submission = store.add_submission(
        review.id, event="APPROVE", body="lgtm", github_review_id=12345
    )
    assert store.list_submissions(review.id) == [submission]
