import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "../lib/api";
import {
  CI_FILTERS,
  DEFAULT_CONTROLS,
  INBOX_NAMES,
  SORT_KEYS,
  arrangeInbox,
  countPrs,
  parseControls,
  prLabel,
  repollDelay,
  type CiFilter,
  type InboxControls,
  type SortKey,
  type StackEntry,
} from "../lib/inbox";
import { decisionLabel, viewerReviewLabel } from "../lib/pr";
import { reviewPath } from "../lib/router";
import { readSetting, writeSetting } from "../lib/storage";
import { formatAge, formatSince } from "../lib/time";
import type { CheckState, InboxList, InboxName } from "../lib/types";
import { Link } from "./Link";
import { errorMessage } from "./Thread";

const CONTROLS_KEY = "code-review-mcp:inbox-controls";

export function useInboxControls(): [InboxControls, (change: Partial<InboxControls>) => void] {
  const [controls, setControls] = useState(() => parseControls(readSetting(CONTROLS_KEY)));
  const update = useCallback((change: Partial<InboxControls>) => {
    setControls((current) => {
      const next = { ...current, ...change };
      writeSetting(CONTROLS_KEY, JSON.stringify(next));
      return next;
    });
  }, []);
  return [controls, update];
}

export interface InboxListState {
  list: InboxList | null;
  loading: boolean;
  error: string | null;
}

const EMPTY_STATE: InboxListState = { list: null, loading: false, error: null };

/**
 * Load each inbox list on its own: `direct` and `mine` together, then `team` once both
 * have answered. A list the daemon is refreshing in the background is asked for again
 * until the fresh copy arrives.
 */
export function useInboxLists() {
  const [states, setStates] = useState<Record<InboxName, InboxListState>>({
    direct: EMPTY_STATE,
    mine: EMPTY_STATE,
    team: EMPTY_STATE,
  });
  const timers = useRef(new Map<InboxName, number>());
  const attempts = useRef(new Map<InboxName, number>());
  const mounted = useRef(true);

  const load = useCallback(async (name: InboxName, refresh = false): Promise<void> => {
    window.clearTimeout(timers.current.get(name));
    if (refresh) attempts.current.set(name, 0);
    setStates((current) => ({ ...current, [name]: { ...current[name], loading: true } }));
    try {
      const list = await api.inboxList(name, refresh);
      if (!mounted.current) return;
      setStates((current) => ({ ...current, [name]: { list, loading: false, error: null } }));
      const attempt = attempts.current.get(name) ?? 0;
      const delay = repollDelay(list, attempt);
      attempts.current.set(name, delay === null ? 0 : attempt + 1);
      if (delay !== null) timers.current.set(name, window.setTimeout(() => void load(name), delay));
    } catch (e) {
      if (!mounted.current) return;
      setStates((current) => ({
        ...current,
        [name]: { ...current[name], loading: false, error: errorMessage(e) },
      }));
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    const pending = timers.current;
    void Promise.allSettled([load("direct"), load("mine")]).then(() => {
      if (mounted.current) void load("team");
    });
    return () => {
      mounted.current = false;
      for (const timer of pending.values()) window.clearTimeout(timer);
    };
  }, [load]);

  const refreshAll = useCallback(
    () => Promise.allSettled(INBOX_NAMES.map((name) => load(name, true))),
    [load],
  );
  return { states, load, refreshAll };
}

const SORT_LABELS: Record<SortKey, string> = {
  updated: "Last updated",
  created: "Newest created",
  author: "Author",
  ci: "CI (failing first)",
  size: "Size (largest first)",
};

const CI_LABELS: Record<CiFilter, string> = {
  any: "Any CI",
  failure: "CI failing",
  pending: "CI pending",
  success: "CI passing",
  none: "No CI",
};

export function InboxControlsBar({
  controls,
  authors,
  onChange,
}: {
  controls: InboxControls;
  authors: readonly string[];
  onChange(change: Partial<InboxControls>): void;
}) {
  const changed = JSON.stringify(controls) !== JSON.stringify(DEFAULT_CONTROLS);
  return (
    <div className="inbox-controls" role="group" aria-label="Sort and filter">
      <input
        type="search"
        className="inbox-search"
        aria-label="Filter text"
        placeholder="Filter: repo, #number, title, author, branch, label"
        value={controls.query}
        onChange={(event) => onChange({ query: event.target.value })}
      />
      <select
        aria-label="Sort"
        value={controls.sort}
        onChange={(event) => onChange({ sort: event.target.value as SortKey })}
      >
        {SORT_KEYS.map((key) => (
          <option key={key} value={key}>
            Sort: {SORT_LABELS[key]}
          </option>
        ))}
      </select>
      <select
        aria-label="Author"
        value={controls.author}
        onChange={(event) => onChange({ author: event.target.value })}
      >
        <option value="">Any author</option>
        {controls.author && !authors.includes(controls.author) && (
          <option value={controls.author}>{controls.author}</option>
        )}
        {authors.map((author) => (
          <option key={author} value={author}>
            {author}
          </option>
        ))}
      </select>
      <select
        aria-label="CI state"
        value={controls.ci}
        onChange={(event) => onChange({ ci: event.target.value as CiFilter })}
      >
        {CI_FILTERS.map((ci) => (
          <option key={ci} value={ci}>
            {CI_LABELS[ci]}
          </option>
        ))}
      </select>
      <label className="check">
        <input
          type="checkbox"
          checked={controls.hideDrafts}
          onChange={(event) => onChange({ hideDrafts: event.target.checked })}
        />
        Hide drafts
      </label>
      <label className="check">
        <input
          type="checkbox"
          checked={controls.hideBots}
          onChange={(event) => onChange({ hideBots: event.target.checked })}
        />
        Hide bots
      </label>
      {changed && (
        <button type="button" className="link-button" onClick={() => onChange(DEFAULT_CONTROLS)}>
          Reset
        </button>
      )}
    </div>
  );
}

const CI_ICONS: Record<string, { icon: string; label: string }> = {
  success: { icon: "✅", label: "CI passing" },
  failure: { icon: "❌", label: "CI failing" },
  pending: { icon: "⏳", label: "CI pending" },
  none: { icon: "—", label: "No CI checks" },
};

function CiIcon({ state }: { state: CheckState | null }) {
  const { icon, label } = CI_ICONS[state ?? "none"] ?? CI_ICONS.none!;
  return (
    <span className="ci-icon" title={label} aria-label={label} role="img">
      {icon}
    </span>
  );
}

function InboxRow({
  entry,
  disabled,
  onOpen,
}: {
  entry: StackEntry;
  disabled: boolean;
  onOpen(ref: string): void;
}) {
  const { item, depth, position, size } = entry;
  const decision = decisionLabel(item.review_decision);
  const yours = viewerReviewLabel(item.viewer_review);
  const content = (
    <>
      <CiIcon state={item.ci_state} />
      <span className="inbox-main">
        {size > 1 && (
          <span className="stack-pos" title={`PR ${position} of ${size} in a stack`}>
            {depth > 0 && "↳ "}
            {position}/{size}
          </span>
        )}
        <span className="pr-ref">{prLabel(item.repo, item.number)}</span>
        <span className="inbox-title" title={item.title}>
          {item.title}
        </span>
        {item.is_draft && <span className="tag">Draft</span>}
        {item.review_id && <span className="tag tag-opened">Opened</span>}
      </span>
      <span className="inbox-author">
        {item.author ?? "unknown"}
        {item.author_is_bot && <span className="tag tag-bot">bot</span>}
      </span>
      <span className="inbox-review">
        {decision && (
          <span className={`tag tag-decision-${item.review_decision?.toLowerCase()}`}>
            {decision}
          </span>
        )}
        {yours && (
          <span className="muted inbox-yours" title={yours}>
            {yours}
          </span>
        )}
      </span>
      <span className="inbox-size" title={`${item.changed_files} files changed`}>
        <span className="count-add">+{item.additions}</span>{" "}
        <span className="count-del">−{item.deletions}</span>{" "}
        <span className="muted">
          · {item.changed_files} {item.changed_files === 1 ? "file" : "files"}
        </span>
      </span>
      <span className="inbox-time muted">
        <time dateTime={item.updated_at} title={`Updated ${item.updated_at}`}>
          {formatAge(item.updated_at)}
        </time>
        {" · "}
        <time dateTime={item.created_at} title={`Created ${item.created_at}`}>
          {formatSince(item.created_at)} old
        </time>
      </span>
    </>
  );
  const className = depth > 0 ? "inbox-row stacked" : "inbox-row";
  return (
    <li>
      {item.review_id ? (
        <Link to={reviewPath(item.review_id)} className={className}>
          {content}
        </Link>
      ) : (
        <button
          type="button"
          className={className}
          disabled={disabled}
          onClick={() => onOpen(item.url)}
        >
          {content}
        </button>
      )}
    </li>
  );
}

function sectionCount(shown: number, list: InboxList): string {
  const loaded = list.items.length;
  const parts = [shown === loaded ? `${loaded}` : `${shown} of ${loaded}`];
  if (list.total > loaded) parts.push(`newest ${loaded} of ${list.total}`);
  return parts.join(" · ");
}

export function InboxSection({
  title,
  state,
  controls,
  disabled,
  onOpen,
  onRetry,
  collapsible = false,
  empty,
  className = "",
}: {
  title: string;
  state: InboxListState;
  controls: InboxControls;
  disabled: boolean;
  onOpen(ref: string): void;
  onRetry(): void;
  collapsible?: boolean;
  empty: string;
  className?: string;
}) {
  const { list, loading, error } = state;
  const groups = useMemo(
    () => (list === null ? [] : arrangeInbox(list.items, controls)),
    [list, controls],
  );
  const shown = countPrs(groups);
  const header = (
    <>
      <h3>{title}</h3>
      <span className="count">{list === null ? "…" : sectionCount(shown, list)}</span>
      {list && (
        <span className="muted section-meta" title={list.fetched_at}>
          {list.refreshing || loading ? "updating…" : `updated ${formatAge(list.fetched_at)}`}
        </span>
      )}
    </>
  );
  let body: ReactNode;
  if (list === null && error !== null) {
    body = (
      <p className="form-error list-empty">
        Could not load this list: {error}{" "}
        <button type="button" className="link-button" onClick={onRetry}>
          Retry
        </button>
      </p>
    );
  } else if (list === null) body = <p className="muted list-empty">Loading…</p>;
  else if (list.items.length === 0) body = <p className="muted list-empty">{empty}</p>;
  else if (shown === 0) body = <p className="muted list-empty">No PRs match the filters.</p>;
  else {
    body = (
      <ul className="review-list inbox-list">
        {groups.flatMap((group) =>
          group.map((entry) => (
            <InboxRow
              key={`${entry.item.repo}#${entry.item.number}`}
              entry={entry}
              disabled={disabled}
              onOpen={onOpen}
            />
          )),
        )}
      </ul>
    );
  }
  const refreshError = list !== null && error !== null && (
    <p className="form-error list-empty">
      Could not refresh this list, so it may be out of date: {error}{" "}
      <button type="button" className="link-button" onClick={onRetry}>
        Retry
      </button>
    </p>
  );
  if (collapsible) {
    return (
      <details className={`inbox-section collapsible-section ${className}`}>
        <summary>{header}</summary>
        {refreshError}
        {body}
      </details>
    );
  }
  return (
    <div className={`inbox-section ${className}`}>
      <div className="section-header">{header}</div>
      {refreshError}
      {body}
    </div>
  );
}
