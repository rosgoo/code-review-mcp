import asyncio
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from code_review_mcp.config import Settings
from code_review_mcp.errors import NotFoundError, ReviewError
from code_review_mcp.github import GitHubClient, PullRequest, ReviewRequest, StatusCheck
from code_review_mcp.hub import ReviewHub
from code_review_mcp.local_files import detect_language
from code_review_mcp.repo_config import RepoConfig, load_repo_config
from code_review_mcp.store import PrKey, PrReviewFields, ReviewRow, Store, utc_now
from code_review_mcp.worktrees import BlobContent, ChangedFile, WorktreeManager


@dataclass(frozen=True)
class OpenedPr:
    review: ReviewRow
    url: str
    note: str | None


@dataclass(frozen=True)
class SyncResult:
    review: ReviewRow
    head_moved: bool
    old_head_sha: str | None
    new_head_sha: str


@dataclass(frozen=True)
class _LiveMetadata:
    pr: PullRequest
    fetched_at: str


class _DiffKey(NamedTuple):
    repo_dir: str
    base: str
    head: str


@dataclass(frozen=True)
class _OpenPr:
    review: ReviewRow
    repo_dir: Path
    head_sha: str
    merge_base_sha: str


def _serialize_check(check: StatusCheck) -> dict[str, object]:
    return {"name": check.name, "state": check.state, "url": check.url, "workflow": check.workflow}


def _serialize_live(live: _LiveMetadata) -> dict[str, object]:
    pr = live.pr
    return {
        "fetched_at": live.fetched_at,
        "review_decision": pr.review_decision,
        "review_requests": list(pr.review_requests),
        "checks_state": pr.checks_state,
        "checks": [_serialize_check(c) for c in pr.checks],
        "additions": pr.additions,
        "deletions": pr.deletions,
    }


def _serialize_changed_file(f: ChangedFile, viewed: bool) -> dict[str, object]:
    return {
        "path": f.path,
        "old_path": f.old_path,
        "status": f.status,
        "additions": f.additions,
        "deletions": f.deletions,
        "binary": f.binary,
        "viewed": viewed,
    }


class PrService:
    """Read-only PR reviews: GitHub metadata through `gh`, file contents from a local worktree."""

    def __init__(
        self,
        store: Store,
        hub: ReviewHub,
        settings: Settings,
        github: GitHubClient,
        worktrees: WorktreeManager,
        config_loader: Callable[[], RepoConfig] | None = None,
    ) -> None:
        self._store = store
        self._hub = hub
        self._settings = settings
        self._github = github
        self._worktrees = worktrees
        self._config_loader = config_loader or (lambda: load_repo_config(settings.home))
        self._live: dict[str, _LiveMetadata] = {}
        self._locks: dict[PrKey, asyncio.Lock] = {}
        self._diff_cache: dict[_DiffKey, list[ChangedFile]] = {}

    def _lock(self, repo: str, number: int) -> asyncio.Lock:
        return self._locks.setdefault(PrKey(repo, number), asyncio.Lock())

    def _require_pr_review(self, review_id: str) -> ReviewRow:
        review = self._store.get_review(review_id)
        if review is None:
            raise NotFoundError(f"Review {review_id!r} not found")
        if review.kind != "pr" or review.repo is None or review.pr_number is None:
            raise ReviewError(f"Review {review_id!r} is not a PR review")
        return review

    def _require_open_pr(self, review_id: str) -> _OpenPr:
        review = self._require_pr_review(review_id)
        if review.status == "closed":
            raise ReviewError(f"Review {review_id!r} is closed. Open the PR again to reload it.")
        if review.worktree_path is None or review.head_sha is None or not review.merge_base_sha:
            raise ReviewError(f"Review {review_id!r} has no worktree yet. Open the PR again.")
        repo_dir = Path(review.worktree_path)
        if not repo_dir.is_dir():
            raise ReviewError(
                f"The worktree of review {review_id!r} is missing. "
                "Refresh the review to recreate it."
            )
        return _OpenPr(
            review=review,
            repo_dir=repo_dir,
            head_sha=review.head_sha,
            merge_base_sha=review.merge_base_sha,
        )

    async def inbox(self, *, refresh: bool = False) -> dict[str, object]:
        """Review requests split into `direct` (the user by name) and `team` (only a team)."""
        snapshot = await self._github.review_requests(refresh=refresh)
        review_ids = self._store.pr_review_ids()

        def serialize(items: Sequence[ReviewRequest]) -> list[dict[str, object]]:
            return [
                {
                    "repo": item.repo,
                    "number": item.number,
                    "title": item.title,
                    "author": item.author,
                    "url": item.url,
                    "updated_at": item.updated_at,
                    "is_draft": item.is_draft,
                    "review_id": review_ids.get(PrKey(item.repo, item.number)),
                }
                for item in items
            ]

        return {
            "fetched_at": snapshot.fetched_at,
            "direct": serialize(snapshot.direct),
            "team": serialize(snapshot.team),
        }

    async def open_pr(self, ref: str) -> OpenedPr:
        """Resolve `ref`, then create or update the PR's review and worktree.

        Opening a PR that already has a review returns that review (same id), moved to the
        PR's current head. A closed review reopens.
        """
        config = self._config_loader()
        resolved = await self._github.resolve_ref(
            ref, default_repo=config.default_repo, search_repos=config.known_repos
        )
        pr = await self._github.pull_request(resolved.repo, resolved.number)
        result = await self._sync(pr, config)
        return OpenedPr(
            review=result.review,
            url=self._settings.review_url(result.review.id),
            note=resolved.note,
        )

    async def refresh(self, review_id: str) -> SyncResult:
        """Re-read the PR from GitHub. If the head moved, check it out and publish head_moved."""
        review = self._require_pr_review(review_id)
        if review.status == "closed":
            raise ReviewError(f"Review {review_id!r} is closed. Open the PR again to reload it.")
        assert review.repo is not None and review.pr_number is not None
        pr = await self._github.pull_request(review.repo, review.pr_number)
        return await self._sync(pr, self._config_loader())

    async def _sync(self, pr: PullRequest, config: RepoConfig) -> SyncResult:
        async with self._lock(pr.repo, pr.number):
            prepared = await self._worktrees.prepare(
                pr.repo,
                pr.number,
                mapped_clone=config.clone_path(pr.repo),
                base_ref=pr.base_ref,
                base_sha=pr.base_sha,
                head_sha=pr.head_sha,
            )
            previous = self._store.find_pr_review(pr.repo, pr.number)
            review = self._store.upsert_pr_review(
                PrReviewFields(
                    repo=pr.repo,
                    pr_number=pr.number,
                    title=pr.title,
                    author=pr.author,
                    url=pr.url,
                    pr_body=pr.body,
                    pr_state=pr.state,
                    is_draft=pr.is_draft,
                    base_ref=pr.base_ref,
                    head_ref=pr.head_ref,
                    base_sha=pr.base_sha,
                    head_sha=prepared.head_sha,
                    merge_base_sha=prepared.merge_base_sha,
                    worktree_path=str(prepared.path),
                )
            )
            self._live[review.id] = _LiveMetadata(pr=pr, fetched_at=utc_now())
        old_head = previous.head_sha if previous is not None else None
        moved = old_head is not None and old_head != prepared.head_sha
        if moved:
            self._hub.publish(
                review.id,
                "head_moved",
                {"old_head_sha": old_head, "new_head_sha": prepared.head_sha},
            )
        return SyncResult(
            review=review,
            head_moved=moved,
            old_head_sha=old_head,
            new_head_sha=prepared.head_sha,
        )

    async def _changed_files(self, pr: _OpenPr) -> list[ChangedFile]:
        key = _DiffKey(str(pr.repo_dir), pr.merge_base_sha, pr.head_sha)
        files = self._diff_cache.get(key)
        if files is None:
            files = await self._worktrees.changed_files(pr.repo_dir, pr.merge_base_sha, pr.head_sha)
            self._diff_cache[key] = files
        return files

    async def _require_changed_file(self, pr: _OpenPr, path: str) -> ChangedFile:
        for changed in await self._changed_files(pr):
            if changed.path == path:
                return changed
        raise NotFoundError(f"{path!r} is not a changed file in this PR")

    async def get_pr_view(self, review_id: str) -> dict[str, object]:
        """The review's PR metadata and changed files. A closed review lists no files.

        The `github` section (CI checks, review decision) is null until the PR has been
        opened or refreshed since the daemon started.
        """
        review = self._require_pr_review(review_id)
        files: list[dict[str, object]] = []
        if review.status != "closed" and review.worktree_path and review.merge_base_sha:
            pr = self._require_open_pr(review_id)
            viewed = self._store.viewed_paths(review_id, pr.head_sha)
            files = [
                _serialize_changed_file(f, f.path in viewed) for f in await self._changed_files(pr)
            ]
        live = self._live.get(review_id)
        return {
            "review_id": review.id,
            "kind": review.kind,
            "url": self._settings.review_url(review.id),
            "status": review.status,
            "repo": review.repo,
            "number": review.pr_number,
            "title": review.title,
            "author": review.author,
            "github_url": review.url,
            "body": review.pr_body,
            "state": review.pr_state,
            "is_draft": review.is_draft,
            "base_ref": review.base_ref,
            "head_ref": review.head_ref,
            "base_sha": review.base_sha,
            "head_sha": review.head_sha,
            "merge_base_sha": review.merge_base_sha,
            "worktree_path": review.worktree_path,
            "github": _serialize_live(live) if live is not None else None,
            "files": files,
        }

    async def get_file(self, review_id: str, path: str) -> dict[str, object]:
        """Old content (at the merge base, under the old path for a rename) and new content
        (at the head) of one changed file.

        Contents are null when a side does not exist, or when the file is binary or too large.
        Raises NotFoundError if `path` is not a changed file of the PR.
        """
        pr = self._require_open_pr(review_id)
        changed = await self._require_changed_file(pr, path)
        old: BlobContent | None = None
        new: BlobContent | None = None
        if not changed.binary:
            old_path = changed.old_path or path
            old, new = await asyncio.gather(
                self._read_side(pr, pr.merge_base_sha, old_path, changed.status != "added"),
                self._read_side(pr, pr.head_sha, path, changed.status != "deleted"),
            )
        sides = [side for side in (old, new) if side is not None]
        binary = changed.binary or any(side.binary for side in sides)
        too_large = any(side.too_large for side in sides)
        return {
            "path": path,
            "old_path": changed.old_path,
            "status": changed.status,
            "language": detect_language(path),
            "merge_base_sha": pr.merge_base_sha,
            "head_sha": pr.head_sha,
            "binary": binary,
            "too_large": too_large,
            "old_content": old.text if old is not None else None,
            "new_content": new.text if new is not None else None,
        }

    async def _read_side(
        self, pr: _OpenPr, sha: str, path: str, exists: bool
    ) -> BlobContent | None:
        if not exists:
            return None
        return await self._worktrees.read_blob(pr.repo_dir, sha, path)

    async def set_viewed(self, review_id: str, path: str, viewed: bool) -> dict[str, object]:
        """Mark or unmark a changed file as viewed at the review's current head SHA."""
        pr = self._require_open_pr(review_id)
        await self._require_changed_file(pr, path)
        self._store.set_file_viewed(review_id, path, pr.head_sha, viewed)
        return {"path": path, "viewed": viewed, "head_sha": pr.head_sha}

    async def close(self, review_id: str) -> None:
        """Remove the review's worktree and namespaced refs and mark it closed.

        Threads and viewed state stay. Opening the PR again reopens the same review.
        """
        review = self._require_pr_review(review_id)
        assert review.repo is not None and review.pr_number is not None
        config = self._config_loader()
        async with self._lock(review.repo, review.pr_number):
            await self._worktrees.remove(
                review.repo, review.pr_number, mapped_clone=config.clone_path(review.repo)
            )
            self._store.close_pr_review(review_id)
        self._live.pop(review_id, None)
        self._hub.publish(review_id, "review_closed")

    async def get_review(self, review_id: str) -> dict[str, object]:
        """The PR view plus a summary of the review's threads."""
        view = await self.get_pr_view(review_id)
        threads = self._store.list_threads(review_id)
        messages = self._store.messages_for_review(review_id)
        view["thread_counts"] = dict(Counter(t.status for t in threads))
        view["threads"] = [
            {
                "id": t.id,
                "kind": t.kind,
                "status": t.status,
                "path": t.path,
                "line": t.line,
                "side": t.side,
                "created_by": t.created_by,
                "message_count": len(messages.get(t.id, [])),
                "first_message": messages[t.id][0].body if messages.get(t.id) else "",
            }
            for t in threads
        ]
        return view
