import asyncio
import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import groupby

from code_review_mcp.errors import ReviewError
from code_review_mcp.github import GitHubClient, fetch_pr_states
from code_review_mcp.pr_service import PrService
from code_review_mcp.repo_config import CleanupConfig, ConfigError, RepoConfig
from code_review_mcp.store import PrKey, ReviewRow, Store

logger = logging.getLogger(__name__)

FIRST_SWEEP_DELAY_SECONDS = 30.0
RECENT_ACTIVITY = timedelta(minutes=60)
FINISHED_STATES = frozenset({"merged", "closed"})


def _utc_clock() -> datetime:
    return datetime.now(UTC)


@dataclass
class SweepReport:
    closed: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    kept_recent: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


class WorktreeSweeper:
    """Frees the worktrees of PR reviews that no longer need them.

    A review of a merged or closed PR is closed (worktree and refs removed), unless it had
    activity in the last RECENT_ACTIVITY. A review of an open PR with no activity for the
    configured idle days keeps its status but releases its worktree; the next read restores it.
    """

    def __init__(
        self,
        store: Store,
        prs: PrService,
        github: GitHubClient,
        config_loader: Callable[[], RepoConfig],
        *,
        clock: Callable[[], datetime] = _utc_clock,
    ) -> None:
        self._store = store
        self._prs = prs
        self._github = github
        self._config_loader = config_loader
        self._clock = clock

    def _cleanup_config(self) -> CleanupConfig:
        return self._config_loader().cleanup

    async def run_forever(self, first_delay: float = FIRST_SWEEP_DELAY_SECONDS) -> None:
        """Sweep after `first_delay` seconds, then every configured interval, until cancelled."""
        await asyncio.sleep(first_delay)
        while True:
            try:
                await self.sweep_once()
            except Exception:  # a bug in one sweep must not stop every later sweep
                logger.exception("worktree cleanup sweep failed")
            try:
                interval = self._cleanup_config().interval_minutes
            except ConfigError:
                interval = CleanupConfig().interval_minutes
            await asyncio.sleep(interval * 60)

    async def sweep_once(self) -> SweepReport:
        """Check every PR review that holds a worktree once. Returns what it did.

        A failure for one repo or one review is logged and the sweep continues.
        """
        report = SweepReport()
        try:
            config = self._cleanup_config()
        except ConfigError as e:
            logger.warning("worktree cleanup skipped: %s", e)
            report.failures.append(str(e))
            return report
        if not config.enabled:
            return report
        now = self._clock()
        reviews = [
            r
            for r in self._store.pr_reviews_with_worktrees()
            if r.repo is not None and r.pr_number is not None
        ]
        states = await self._fetch_states(reviews, report)
        for review in reviews:
            label = f"{review.repo}#{review.pr_number}"
            try:
                await self._sweep_review(review, label, states, config, now, report)
            except (ReviewError, OSError, sqlite3.Error) as e:
                logger.warning("worktree cleanup of %s failed: %s", label, e)
                report.failures.append(f"{label}: {e}")
        return report

    async def _fetch_states(
        self, reviews: Sequence[ReviewRow], report: SweepReport
    ) -> dict[PrKey, str]:
        states: dict[PrKey, str] = {}
        for repo, group in groupby(reviews, key=lambda r: r.repo or ""):
            numbers = [r.pr_number for r in group if r.pr_number is not None]
            try:
                fetched = await fetch_pr_states(self._github, repo, numbers)
            except ReviewError as e:
                logger.warning("worktree cleanup could not read PR states for %s: %s", repo, e)
                report.failures.append(f"{repo}: {e}")
                continue
            for number, state in fetched.items():
                states[PrKey(repo, number)] = state
        return states

    async def _sweep_review(
        self,
        review: ReviewRow,
        label: str,
        states: dict[PrKey, str],
        config: CleanupConfig,
        now: datetime,
        report: SweepReport,
    ) -> None:
        assert review.repo is not None and review.pr_number is not None
        state = states.get(PrKey(review.repo, review.pr_number))
        if state is not None and state != review.pr_state:
            self._store.set_pr_state(review.id, state)
        if state in FINISHED_STATES:
            if await self._prs.close_if_inactive(review.id, now - RECENT_ACTIVITY):
                logger.info("removed worktree of %s: the PR is %s", label, state)
                report.closed.append(label)
            else:
                logger.info(
                    "kept worktree of %s: the PR is %s, but the review was active in the "
                    "last %d minutes",
                    label,
                    state,
                    int(RECENT_ACTIVITY.total_seconds() // 60),
                )
                report.kept_recent.append(label)
            return
        if await self._prs.release_if_inactive(review.id, now - timedelta(days=config.idle_days)):
            logger.info("released worktree of %s: no activity for %g days", label, config.idle_days)
            report.released.append(label)
