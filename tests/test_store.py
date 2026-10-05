import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from code_review_mcp import store as store_module
from code_review_mcp.config import Settings
from code_review_mcp.store import (
    MIGRATIONS,
    PrKey,
    PrReviewFields,
    ReviewFile,
    SchemaVersionError,
    Store,
    ThreadStatus,
)

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
        "base_ref",
        "head_ref",
        "pr_body",
        "pr_state",
        "is_draft",
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
    "viewed_files": {"review_id", "path", "head_sha", "viewed_at"},
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


PR_FIELDS = PrReviewFields(
    repo="o/r",
    pr_number=7,
    title="PR seven",
    author="octocat",
    url="https://github.com/o/r/pull/7",
    pr_body="body",
    pr_state="open",
    is_draft=True,
    base_ref="main",
    head_ref="feature",
    base_sha="b" * 40,
    head_sha="h" * 40,
    merge_base_sha="m" * 40,
    worktree_path="/wt/o-r-7",
)


def test_migration_2_upgrades_a_version_1_database(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(f"BEGIN;\n{MIGRATIONS[0]}\nPRAGMA user_version = 1;\nCOMMIT;")
    now = "2026-10-01T00:00:00+00:00"
    conn.executemany(
        "INSERT INTO reviews (id, kind, title, repo, pr_number, mode, patch_text,"
        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("local1", "local", "Local one", None, None, "diff", "d", now, now),
            ("local2", "local", "Local two", None, None, "diff", "d", now, now),
            ("pr-null1", "pr", "PR placeholder", None, None, None, None, now, now),
            ("pr-null2", "pr", "PR placeholder", None, None, None, None, now, now),
        ],
    )
    conn.commit()
    conn.close()

    store = Store.open(db_path)
    try:
        assert store.schema_version == 2
        local = store.get_review("local1")
        assert local is not None
        assert (local.title, local.patch_text, local.is_draft) == ("Local one", "d", False)
        assert (local.base_ref, local.head_ref, local.pr_body, local.pr_state) == (
            None,
            None,
            None,
            None,
        )
        assert {r.id for r in store.list_reviews()} == {"local1", "local2", "pr-null1", "pr-null2"}

        created = store.upsert_pr_review(PR_FIELDS)
        assert created.is_draft is True
        with pytest.raises(sqlite3.IntegrityError):
            store._conn.execute(
                "INSERT INTO reviews (id, kind, title, repo, pr_number, created_at, updated_at)"
                " VALUES ('dup', 'pr', 'dup', 'o/r', 7, ?, ?)",
                (now, now),
            )
        store._conn.execute(
            "INSERT INTO reviews (id, kind, title, repo, pr_number, created_at, updated_at)"
            " VALUES ('local-same-pr', 'local', 'x', 'o/r', 7, ?, ?)",
            (now, now),
        )
    finally:
        store.close()
    for table, columns in EXPECTED_COLUMNS.items():
        assert _columns(db_path, table) == columns


def test_upsert_pr_review_keeps_id_and_reopens(store: Store) -> None:
    created = store.upsert_pr_review(PR_FIELDS)
    other = store.upsert_pr_review(replace(PR_FIELDS, pr_number=8))
    store.close_pr_review(created.id)
    closed = store.get_review(created.id)
    assert closed is not None
    assert (closed.status, closed.worktree_path) == ("closed", None)

    updated = store.upsert_pr_review(
        replace(PR_FIELDS, title="Renamed", head_sha="n" * 40, is_draft=False, pr_state="merged")
    )

    assert updated.id == created.id
    assert updated.created_at == created.created_at
    assert (updated.title, updated.head_sha, updated.is_draft, updated.pr_state) == (
        "Renamed",
        "n" * 40,
        False,
        "merged",
    )
    assert updated.status == "open"
    assert updated.worktree_path == "/wt/o-r-7"
    assert store.find_pr_review("o/r", 7) == updated
    assert store.find_pr_review("o/r", 9) is None
    assert store.pr_review_ids() == {PrKey("o/r", 7): created.id, PrKey("o/r", 8): other.id}

    store._conn.execute("UPDATE reviews SET status = 'submitted' WHERE id = ?", (created.id,))
    kept = store.upsert_pr_review(PR_FIELDS)
    assert kept.status == "submitted"


def test_viewed_files(store: Store) -> None:
    review = store.upsert_pr_review(PR_FIELDS)

    store.set_file_viewed(review.id, "a.py", "h1", True)
    store.set_file_viewed(review.id, "a.py", "h1", True)
    store.set_file_viewed(review.id, "b.py", "h1", True)
    store.set_file_viewed(review.id, "a.py", "h2", True)
    store.set_file_viewed(review.id, "b.py", "h1", False)

    assert store.viewed_paths(review.id, "h1") == {"a.py"}
    assert store.viewed_paths(review.id, "h2") == {"a.py"}
    assert store.viewed_paths(review.id, "h3") == set()
    with pytest.raises(sqlite3.IntegrityError):
        store.set_file_viewed("no-such-review", "a.py", "h1", True)
