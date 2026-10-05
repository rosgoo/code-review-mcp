import type { DiffFileInput, DiffLineAnnotation, SelectedLineRange } from "@pierre/diffs";
import { MultiFileDiff, useStableCallback, type FileDiffOptions } from "@pierre/diffs/react";
import { memo, useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import type { AnnotationData } from "../lib/annotations";
import { useNearViewport } from "../lib/hooks";
import { estimatedDiffHeight, statusLetter } from "../lib/pr";
import {
  clampSelection,
  isCommentable,
  reviewAnnotations,
  type PlacedThreads,
} from "../lib/review";
import type {
  Commentable,
  DiffStyle,
  PrFile,
  PrFileContent,
  ReviewAnchor,
  Side,
} from "../lib/types";
import { fileDomId } from "./FileViews";
import { usePrCommentActions } from "./PrCommentContext";
import { FileThreadsBlock, ReviewAnnotation } from "./ReviewThreads";

export type FileLoad =
  | { state: "loading" }
  | { state: "loaded"; content: PrFileContent }
  | { state: "error"; message: string };

function diffInput(content: PrFileContent): DiffFileInput | null {
  const oldFile =
    content.old_content === null
      ? null
      : { name: content.old_path ?? content.path, contents: content.old_content };
  const newFile =
    content.new_content === null ? null : { name: content.path, contents: content.new_content };
  if (oldFile !== null && newFile !== null) return { oldFile, newFile };
  if (newFile !== null) return { oldFile: null, newFile };
  if (oldFile !== null) return { oldFile, newFile: null };
  return null;
}

function Placeholder({ children, height }: { children: ReactNode; height?: number }) {
  return (
    <div
      className="file-placeholder"
      style={height === undefined ? undefined : { minHeight: height }}
    >
      {children}
    </div>
  );
}

interface HoveredLine {
  lineNumber: number;
  side: Side;
}

/**
 * The last line the pointer entered. Leaving a line does not clear it, because moving
 * onto the + button leaves the line, and the button must stay to be clicked.
 */
function createHoverStore() {
  let line: HoveredLine | null = null;
  const listeners = new Set<() => void>();
  return {
    enter(next: HoveredLine) {
      if (line !== null && line.lineNumber === next.lineNumber && line.side === next.side) return;
      line = next;
      for (const listener of listeners) listener();
    },
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    get: () => line,
  };
}

type HoverStore = ReturnType<typeof createHoverStore>;

function GutterPlus({
  hover,
  commentable,
  onPick,
}: {
  hover: HoverStore;
  commentable: Commentable | undefined;
  onPick(range: SelectedLineRange): void;
}) {
  const line = useSyncExternalStore(hover.subscribe, hover.get);
  const button = useRef<HTMLButtonElement>(null);
  const latest = useRef({ line, onPick });
  useEffect(() => {
    latest.current = { line, onPick };
  });
  useEffect(() => {
    const element = button.current;
    if (element === null) return;
    // The diff listens on its own <pre>, so stopping here keeps a press on + from also
    // starting the diff's line selection. A press picks the line it starts on; a drag
    // picks the range up to the last line the pointer entered before release.
    const press = (event: PointerEvent) => {
      event.stopPropagation();
      if (event.button !== 0) return;
      event.preventDefault();
      const start = latest.current.line;
      if (start === null) return;
      const release = () => {
        const end = hover.get() ?? start;
        latest.current.onPick({
          start: start.lineNumber,
          side: start.side,
          end: end.lineNumber,
          endSide: end.side,
        });
      };
      document.addEventListener("pointerup", release, { once: true });
    };
    const click = (event: MouseEvent) => {
      event.stopPropagation();
      const picked = latest.current.line;
      if (event.detail === 0 && picked !== null) {
        latest.current.onPick({ start: picked.lineNumber, end: picked.lineNumber, side: picked.side });
      }
    };
    element.addEventListener("pointerdown", press);
    element.addEventListener("click", click);
    return () => {
      element.removeEventListener("pointerdown", press);
      element.removeEventListener("click", click);
    };
  });
  if (line === null || !isCommentable(commentable, line.side, line.lineNumber)) return null;
  return (
    <button
      ref={button}
      type="button"
      className="gutter-plus"
      aria-label="Add a review comment"
      title="Add a review comment"
    >
      +
    </button>
  );
}

const renderReviewAnnotation = (annotation: DiffLineAnnotation<AnnotationData>) => (
  <ReviewAnnotation data={annotation.metadata} />
);

function FileDiffView({
  path,
  content,
  diffStyle,
  placed,
  composer,
  commenting,
}: {
  path: string;
  content: PrFileContent;
  diffStyle: DiffStyle;
  placed: PlacedThreads | undefined;
  composer: ReviewAnchor | null;
  commenting: boolean;
}) {
  const actions = usePrCommentActions();
  const input = useMemo(() => diffInput(content), [content]);
  const hover = useMemo(createHoverStore, []);
  // The diff keeps a line selection of its own and pins the + to its end. Clear it after
  // a pick that opens no line composer here, and when this file's composer closes, so
  // the + follows the pointer again.
  const [clearSelection, setClearSelection] = useState(false);
  const [picks, setPicks] = useState(0);
  const awaitingPick = useRef(false);
  const previousComposer = useRef(composer);
  useEffect(() => {
    const openedHere = composer !== null && composer.line > 0;
    const closed = previousComposer.current !== null && composer === null;
    previousComposer.current = composer;
    if (closed || (awaitingPick.current && !openedHere)) setClearSelection(true);
    awaitingPick.current = false;
  }, [composer, picks]);
  useEffect(() => {
    if (clearSelection) setClearSelection(false);
  }, [clearSelection]);
  const onRange = useStableCallback((range: SelectedLineRange) => {
    actions.pickLines(path, clampSelection(path, range, content.commentable));
    awaitingPick.current = true;
    setPicks((count) => count + 1);
  });
  const options = useMemo<FileDiffOptions<AnnotationData, undefined>>(
    () => ({
      diffStyle,
      disableFileHeader: true,
      ...(commenting
        ? {
            enableGutterUtility: true,
            enableLineSelection: true,
            onLineSelected: (range: SelectedLineRange | null) => {
              if (range !== null) onRange(range);
            },
            onLineEnter: ({ lineNumber, annotationSide }: { lineNumber: number; annotationSide: Side }) =>
              hover.enter({ lineNumber, side: annotationSide }),
          }
        : {}),
    }),
    [diffStyle, commenting, onRange, hover],
  );
  const lineAnnotations = useMemo(
    () => reviewAnnotations(placed?.inline ?? [], composer),
    [placed, composer],
  );

  if (input === null) return <Placeholder>No content.</Placeholder>;
  if (
    input.oldFile !== null &&
    input.newFile !== null &&
    input.oldFile.contents === input.newFile.contents
  ) {
    return <Placeholder>No content changes.</Placeholder>;
  }
  return (
    <MultiFileDiff
      {...input}
      options={options}
      selectedLines={clearSelection ? null : undefined}
      lineAnnotations={lineAnnotations}
      renderAnnotation={renderReviewAnnotation}
      renderGutterUtility={
        commenting
          ? () => (
              <GutterPlus
                hover={hover}
                commentable={content.commentable}
                onPick={onRange}
              />
            )
          : undefined
      }
    />
  );
}

function FileBody({
  file,
  load,
  diffStyle,
  placed,
  composer,
  commenting,
  onRetry,
}: {
  file: PrFile;
  load: FileLoad | undefined;
  diffStyle: DiffStyle;
  placed: PlacedThreads | undefined;
  composer: ReviewAnchor | null;
  commenting: boolean;
  onRetry(): void;
}) {
  let diff: ReactNode;
  if (file.binary) diff = <Placeholder>Binary file not shown.</Placeholder>;
  else if (load === undefined || load.state === "loading") {
    diff = <Placeholder height={estimatedDiffHeight(file)}>Loading…</Placeholder>;
  } else if (load.state === "error") {
    diff = (
      <Placeholder>
        <span className="form-error">{load.message}</span>{" "}
        <button type="button" className="link-button" onClick={onRetry}>
          Retry
        </button>
      </Placeholder>
    );
  } else if (load.content.binary) diff = <Placeholder>Binary file not shown.</Placeholder>;
  else if (load.content.too_large) {
    diff = <Placeholder>This file is too large to show here. Open it on GitHub.</Placeholder>;
  } else {
    diff = (
      <FileDiffView
        path={file.path}
        content={load.content}
        diffStyle={diffStyle}
        placed={placed}
        composer={composer}
        commenting={commenting}
      />
    );
  }
  return (
    <>
      <FileThreadsBlock threads={placed?.block ?? []} composer={composer} />
      {diff}
    </>
  );
}

interface PrFileCardProps {
  file: PrFile;
  head: string;
  load: FileLoad | undefined;
  collapsed: boolean;
  diffStyle: DiffStyle;
  scrollRoot: Element | null;
  placed: PlacedThreads | undefined;
  /** The open composer when it belongs to this file, else null. */
  composer: ReviewAnchor | null;
  commenting: boolean;
  onLoad(path: string, head: string, force?: boolean): void;
  onToggleCollapsed(file: PrFile): void;
  onToggleViewed(path: string, viewed: boolean): void;
}

/**
 * One changed file. Its contents load the first time the card is expanded within
 * reach of the visible area, so a large PR does not fetch every file at once.
 */
export const PrFileCard = memo(function PrFileCard({
  file,
  head,
  load,
  collapsed,
  diffStyle,
  scrollRoot,
  placed,
  composer,
  commenting,
  onLoad,
  onToggleCollapsed,
  onToggleViewed,
}: PrFileCardProps) {
  const actions = usePrCommentActions();
  const ref = useRef<HTMLElement>(null);
  const near = useNearViewport(ref, scrollRoot, "600px");
  const wantsContent = near && !collapsed && !file.binary;
  const threadCount = (placed?.inline.length ?? 0) + (placed?.block.length ?? 0);

  useEffect(() => {
    if (wantsContent && load === undefined) onLoad(file.path, head);
  }, [wantsContent, load, file.path, head, onLoad]);

  return (
    <section
      ref={ref}
      id={fileDomId(file.path)}
      className={collapsed ? "file-card pr-file collapsed" : "file-card pr-file"}
    >
      <header className="pr-file-header">
        <button
          type="button"
          className="collapse-button"
          aria-expanded={!collapsed}
          aria-label={collapsed ? "Expand file" : "Collapse file"}
          onClick={() => onToggleCollapsed(file)}
        >
          {collapsed ? "▸" : "▾"}
        </button>
        <span className={`status-letter status-${file.status}`} title={file.status}>
          {statusLetter(file.status)}
        </span>
        <button
          type="button"
          className="pr-file-path"
          title={file.old_path ? `${file.old_path} → ${file.path}` : file.path}
          onClick={() => onToggleCollapsed(file)}
        >
          {file.old_path ? (
            <>
              <span className="muted">{file.old_path}</span> → {file.path}
            </>
          ) : (
            file.path
          )}
        </button>
        <span className="tree-counts">
          {file.binary ? (
            <span className="muted">binary</span>
          ) : (
            <>
              {(file.additions ?? 0) > 0 && <span className="count-add">+{file.additions}</span>}
              {(file.deletions ?? 0) > 0 && <span className="count-del">−{file.deletions}</span>}
            </>
          )}
        </span>
        {threadCount > 0 && (
          <span className="tag" title="Review comments on this file">
            {threadCount} comment{threadCount === 1 ? "" : "s"}
          </span>
        )}
        <span className="spacer" />
        {commenting && (
          <button
            type="button"
            className="button button-small"
            onClick={() => {
              if (collapsed) onToggleCollapsed(file);
              actions.openComposer({ path: file.path, side: "additions", line: 0 });
            }}
          >
            Comment on file
          </button>
        )}
        <label className="viewed-toggle">
          <input
            type="checkbox"
            checked={file.viewed}
            onChange={(event) => onToggleViewed(file.path, event.target.checked)}
          />
          Viewed
        </label>
      </header>
      {!collapsed && (
        <FileBody
          file={file}
          load={load}
          diffStyle={diffStyle}
          placed={placed}
          composer={composer}
          commenting={commenting}
          onRetry={() => onLoad(file.path, head, true)}
        />
      )}
    </section>
  );
});
