import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChecksSummary } from "../components/ChecksSummary";
import { CopyButton } from "../components/CopyButton";
import { DiffStyleToggle } from "../components/DiffStyleToggle";
import { fileDomId } from "../components/FileViews";
import { FileTree, type FileDetail } from "../components/FileTree";
import { Link } from "../components/Link";
import { Markdown } from "../components/Markdown";
import { PrFileCard, type FileLoad } from "../components/PrFileCard";
import { errorMessage } from "../components/Thread";
import { api } from "../lib/api";
import { jumpTo } from "../lib/dom";
import { useDiffStyle, useElapsedSeconds, useReviewEvents } from "../lib/hooks";
import { prLabel, shellQuote } from "../lib/inbox";
import type { ChangeCounts } from "../lib/patch";
import {
  decisionLabel,
  followViewed,
  headMovedBanner,
  isCollapsed,
  keepHead,
  loadKey,
  setFileViewed,
  toggleCollapsed,
  type CollapseOverrides,
  type HeadMovedEvent,
} from "../lib/pr";
import { navigationState } from "../lib/router";
import type { PrFile, PrView } from "../lib/types";

type Busy = "refresh" | "reload" | "close" | "reopen";

const BUSY_TEXT: Record<Busy, string> = {
  refresh: "Refreshing from GitHub…",
  reload: "Reloading…",
  close: "Removing the local worktree…",
  reopen: "Reopening: fetching the PR and checking out a worktree…",
};

function PrHeader({ pr }: { pr: PrView }) {
  const decision = decisionLabel(pr.github?.review_decision ?? null);
  const state = pr.state ?? "open";
  return (
    <div className="pr-meta">
      {pr.author && <span>by {pr.author}</span>}
      {pr.base_ref && pr.head_ref && (
        <code className="pr-refs" title={`${pr.head_ref} into ${pr.base_ref}`}>
          {pr.base_ref} ← {pr.head_ref}
        </code>
      )}
      {pr.is_draft && <span className="tag">Draft</span>}
      <span className={`tag tag-state-${state}`}>{state}</span>
      {decision && (
        <span className={`tag tag-decision-${pr.github?.review_decision?.toLowerCase()}`}>
          {decision}
        </span>
      )}
      {pr.github && pr.github.review_requests.length > 0 && (
        <span className="muted">Requested: {pr.github.review_requests.join(", ")}</span>
      )}
      {pr.head_sha && (
        <code className="muted" title={pr.head_sha}>
          head {pr.head_sha.slice(0, 7)}
        </code>
      )}
    </div>
  );
}

function WorktreeRow({ path }: { path: string }) {
  return (
    <div className="worktree-row">
      <span className="muted">Worktree</span>
      <code className="worktree-path" title={path}>
        {path}
      </code>
      <CopyButton text={path} label="Copy path" />
      <CopyButton text={`cd ${shellQuote(path)} && claude`} label="Copy cd && claude" />
      <span className="muted worktree-note">
        The daemon force-checks-out this worktree when new commits arrive, so edits made there
        are lost.
      </span>
    </div>
  );
}

export function PrReviewPage({ reviewId }: { reviewId: string }) {
  const [pr, setPr] = useState<PrView | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [diffStyle, setDiffStyle] = useDiffStyle();
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [overrides, setOverrides] = useState<CollapseOverrides>(() => new Map());
  const [loads, setLoads] = useState<ReadonlyMap<string, FileLoad>>(() => new Map());
  const [latestMove, setLatestMove] = useState<HeadMovedEvent | null>(null);
  const [busy, setBusy] = useState<{ kind: Busy; startedAt: number } | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmingClose, setConfirmingClose] = useState(false);
  const [note, setNote] = useState<string | null>(() => navigationState().note ?? null);
  const [scrollRoot, setScrollRoot] = useState<HTMLElement | null>(null);
  const requested = useRef(new Set<string>());
  const elapsed = useElapsedSeconds(busy?.startedAt ?? null);

  const loadPr = useCallback(async () => {
    try {
      setPr(await api.prView(reviewId));
      setLoadError(null);
    } catch (e) {
      setLoadError(errorMessage(e));
    }
  }, [reviewId]);

  useEffect(() => {
    void loadPr();
  }, [loadPr]);

  useReviewEvents(reviewId, (event) => {
    if (event.type === "head_moved") setLatestMove(event);
    else if (event.type === "review_closed" || event.type === "view_updated") void loadPr();
  });

  useEffect(() => {
    document.title = pr
      ? `${prLabel(pr.repo, pr.number)} ${pr.title} · Code Review`
      : "Code Review";
  }, [pr]);

  const loadFile = useCallback(
    async (path: string, head: string, force = false) => {
      const key = loadKey(head, path);
      if (requested.current.has(key) && !force) return;
      requested.current.add(key);
      setLoads((current) => new Map(current).set(key, { state: "loading" }));
      let entry: FileLoad;
      try {
        const content = await api.prFile(reviewId, path);
        entry =
          content.head_sha === head
            ? { state: "loaded", content }
            : { state: "error", message: "The PR has new commits. Reload to see this file." };
      } catch (e) {
        requested.current.delete(key);
        entry = { state: "error", message: errorMessage(e) };
      }
      setLoads((current) => new Map(current).set(key, entry));
    },
    [reviewId],
  );

  const shownHead = pr !== null && pr.status !== "closed" ? pr.head_sha : null;

  useEffect(() => {
    const prefix = shownHead === null ? null : `${shownHead}:`;
    for (const key of requested.current) {
      if (prefix === null || !key.startsWith(prefix)) requested.current.delete(key);
    }
    setLoads((current) => keepHead(current, shownHead));
    setOverrides(new Map());
  }, [shownHead]);

  async function run(kind: Busy, action: () => Promise<void>) {
    setBusy({ kind, startedAt: Date.now() });
    setActionError(null);
    try {
      await action();
    } catch (e) {
      setActionError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  const refresh = () =>
    run("refresh", async () => {
      const result = await api.refreshPr(reviewId);
      setPr(result.pr);
    });

  const reload = () =>
    run("reload", async () => {
      setLatestMove(null);
      await loadPr();
    });

  const closeReview = () =>
    run("close", async () => {
      setConfirmingClose(false);
      await api.closePr(reviewId);
      await loadPr();
    });

  const reopen = (target: string) =>
    run("reopen", async () => {
      await api.openPr(target);
      await loadPr();
    });

  const toggleViewed = useCallback(
    async (path: string, viewed: boolean) => {
      setPr(
        (current) => current && { ...current, files: setFileViewed(current.files, path, viewed) },
      );
      setOverrides((current) => followViewed(current, path));
      try {
        await api.setViewed(reviewId, path, viewed);
      } catch (e) {
        setPr(
          (current) =>
            current && { ...current, files: setFileViewed(current.files, path, !viewed) },
        );
        setActionError(errorMessage(e));
      }
    },
    [reviewId],
  );

  const onToggleViewed = useCallback(
    (path: string, viewed: boolean) => void toggleViewed(path, viewed),
    [toggleViewed],
  );
  const onToggleCollapsed = useCallback((file: PrFile) => {
    setOverrides((current) => toggleCollapsed(current, file));
  }, []);
  const onLoad = useCallback(
    (path: string, head: string, force?: boolean) => void loadFile(path, head, force),
    [loadFile],
  );

  const files = useMemo(() => pr?.files ?? [], [pr]);
  const paths = useMemo(() => files.map((f) => f.path), [files]);
  const counts = useMemo(
    () =>
      new Map<string, ChangeCounts>(
        files.map((f) => [f.path, { additions: f.additions ?? 0, deletions: f.deletions ?? 0 }]),
      ),
    [files],
  );
  const details = useMemo(
    () =>
      new Map<string, FileDetail>(
        files.map((f) => [f.path, { status: f.status, oldPath: f.old_path, viewed: f.viewed }]),
      ),
    [files],
  );
  const viewedCount = files.filter((f) => f.viewed).length;
  const banner = headMovedBanner(pr?.head_sha ?? null, latestMove);
  const reopenTarget = pr ? pr.github_url ?? prLabel(pr.repo, pr.number) : "";

  let body;
  if (loadError !== null && pr === null) {
    body = (
      <div className="empty-state">
        <p>Could not load this PR review: {loadError}</p>
        <Link to="/">Back to all reviews</Link>
      </div>
    );
  } else if (pr === null) {
    body = <div className="empty-state muted">Loading…</div>;
  } else if (pr.status === "closed") {
    body = (
      <div className="empty-state">
        <p>This review is closed. Its local worktree was removed.</p>
        <button
          type="button"
          className="button primary"
          disabled={busy !== null}
          onClick={() => void reopen(reopenTarget)}
        >
          Reopen review
        </button>
      </div>
    );
  } else if (files.length === 0) {
    body = <div className="empty-state muted">This PR changes no files.</div>;
  } else {
    const head = pr.head_sha ?? "";
    body = files.map((file) => (
      <PrFileCard
        key={`${head}:${file.path}`}
        file={file}
        head={head}
        load={loads.get(loadKey(head, file.path))}
        collapsed={isCollapsed(file, overrides)}
        diffStyle={diffStyle}
        scrollRoot={scrollRoot}
        onLoad={onLoad}
        onToggleCollapsed={onToggleCollapsed}
        onToggleViewed={onToggleViewed}
      />
    ));
  }

  return (
    <div className="pr-page">
      <header className="topbar">
        <button
          type="button"
          className="icon-button"
          aria-label="Toggle file tree"
          aria-pressed={sidebarOpen}
          onClick={() => setSidebarOpen(!sidebarOpen)}
        >
          ☰
        </button>
        <Link to="/" className="topbar-home">
          Reviews
        </Link>
        <span className="topbar-sep">/</span>
        {pr &&
          (pr.github_url ? (
            <a className="pr-ref" href={pr.github_url} target="_blank" rel="noreferrer noopener">
              {prLabel(pr.repo, pr.number)}
            </a>
          ) : (
            <span className="pr-ref">{prLabel(pr.repo, pr.number)}</span>
          ))}
        <h1 className="topbar-title" title={pr?.title}>
          {pr?.title ?? "…"}
        </h1>
        <span className="spacer" />
        {pr && pr.status !== "closed" && (
          <>
            <DiffStyleToggle value={diffStyle} onChange={setDiffStyle} />
            <button
              type="button"
              className="button"
              disabled={busy !== null}
              onClick={() => void refresh()}
            >
              Refresh
            </button>
            <button
              type="button"
              className="button danger-outline"
              disabled={busy !== null}
              aria-expanded={confirmingClose}
              onClick={() => setConfirmingClose(!confirmingClose)}
            >
              Close review
            </button>
          </>
        )}
      </header>

      <div className="pr-header">
        {pr && <PrHeader pr={pr} />}
        {pr && pr.status !== "closed" && <ChecksSummary github={pr.github} />}
        {pr && pr.status !== "closed" && pr.worktree_path && (
          <WorktreeRow path={pr.worktree_path} />
        )}
        {confirmingClose && (
          <div className="banner banner-warning" role="alertdialog" aria-label="Close review">
            <span>
              Close this review? This removes the local worktree (about 650 MB for Maybern). Viewed
              state stays, and opening the PR again restores it.
            </span>
            <span className="spacer" />
            <button type="button" className="button" onClick={() => setConfirmingClose(false)}>
              Cancel
            </button>
            <button type="button" className="button danger" onClick={() => void closeReview()}>
              Close review
            </button>
          </div>
        )}
        {banner && (
          <div className="banner banner-info" role="status">
            <span>
              New commits: <code>{banner.from}</code>..<code>{banner.to}</code>
            </span>
            <span className="spacer" />
            <button
              type="button"
              className="button primary"
              disabled={busy !== null}
              onClick={() => void reload()}
            >
              Reload
            </button>
          </div>
        )}
        {busy && (
          <div className="banner banner-info" role="status">
            {BUSY_TEXT[busy.kind]} {elapsed > 0 && <span className="muted">{elapsed} s</span>}
          </div>
        )}
        {note && (
          <div className="banner banner-info" role="status">
            <span>{note}</span>
            <span className="spacer" />
            <button
              type="button"
              className="icon-button"
              aria-label="Dismiss"
              onClick={() => setNote(null)}
            >
              ×
            </button>
          </div>
        )}
        {(actionError || (loadError && pr)) && (
          <div className="banner banner-error" role="alert">
            {actionError ?? loadError}
          </div>
        )}
      </div>

      <div className={sidebarOpen ? "review-body" : "review-body no-sidebar"}>
        {sidebarOpen && (
          <FileTree
            paths={paths}
            counts={counts}
            details={details}
            onSelect={(path) => jumpTo(fileDomId(path), "start")}
            onToggleViewed={onToggleViewed}
          />
        )}
        <main className="review-main" ref={setScrollRoot}>
          {pr && pr.status !== "closed" && files.length > 0 && (
            <div className="pr-summary muted">
              {files.length} files · {viewedCount} viewed
            </div>
          )}
          {pr?.body && pr.status !== "closed" && (
            <details className="pr-description">
              <summary>Description</summary>
              <Markdown text={pr.body} />
            </details>
          )}
          {body}
        </main>
      </div>
    </div>
  );
}
