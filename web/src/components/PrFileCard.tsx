import type { DiffFileInput, DiffLineAnnotation, SelectedLineRange } from "@pierre/diffs";
import { MultiFileDiff, useStableCallback, type FileDiffOptions } from "@pierre/diffs/react";
import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import {
  composerAnchor,
  fileComposer,
  inHunks,
  splitByHunks,
  type Composer,
} from "../lib/agent";
import type { AnnotationData } from "../lib/annotations";
import { useNearViewport } from "../lib/hooks";
import { estimatedDiffHeight, statusLetter } from "../lib/pr";
import { isCommentable, reviewAnnotations, type PlacedThreads } from "../lib/review";
import type {
  Commentable,
  ComposerMode,
  DiffStyle,
  PrFile,
  PrFileContent,
  ReviewThread,
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
  agentOn,
  onDrag,
  onPick,
}: {
  hover: HoverStore;
  commentable: Commentable | undefined;
  agentOn: boolean;
  onDrag(range: SelectedLineRange | null): void;
  onPick(range: SelectedLineRange): void;
}) {
  const line = useSyncExternalStore(hover.subscribe, hover.get);
  const button = useRef<HTMLButtonElement>(null);
  const latest = useRef({ line, onDrag, onPick });
  useEffect(() => {
    latest.current = { line, onDrag, onPick };
  });
  useEffect(() => {
    const element = button.current;
    if (element === null) return;
    // The diff listens on its own <pre>, so stopping here keeps a press on + from also
    // starting the diff's line selection. A press picks the line it starts on; a drag
    // picks the range up to the last line the pointer entered before release, and
    // reports each range on the way so the diff can highlight it.
    const press = (event: PointerEvent) => {
      event.stopPropagation();
      if (event.button !== 0) return;
      event.preventDefault();
      const start = latest.current.line;
      if (start === null) return;
      const rangeTo = (end: HoveredLine): SelectedLineRange => ({
        start: start.lineNumber,
        side: start.side,
        end: end.lineNumber,
        endSide: end.side,
      });
      latest.current.onDrag(rangeTo(start));
      const unsubscribe = hover.subscribe(() => {
        latest.current.onDrag(rangeTo(hover.get() ?? start));
      });
      const listening = new AbortController();
      const finish = (picked: boolean) => {
        listening.abort();
        unsubscribe();
        const range = rangeTo(hover.get() ?? start);
        latest.current.onDrag(null);
        if (picked) latest.current.onPick(range);
      };
      document.addEventListener("pointerup", () => finish(true), { signal: listening.signal });
      document.addEventListener("pointercancel", () => finish(false), {
        signal: listening.signal,
      });
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
  if (line === null) return null;
  // Review comments go only on lines inside the diff; questions to the agent go anywhere.
  const inDiff = isCommentable(commentable, line.side, line.lineNumber);
  if (!inDiff && !agentOn) return null;
  const label = inDiff ? "Add a review comment or ask the agent" : "Ask the agent about this line";
  return (
    <button
      ref={button}
      type="button"
      className={inDiff ? "gutter-plus" : "gutter-plus gutter-ask"}
      aria-label={label}
      title={label}
    >
      {inDiff ? "+" : "?"}
    </button>
  );
}

const renderReviewAnnotation = (annotation: DiffLineAnnotation<AnnotationData>) => (
  <ReviewAnnotation data={annotation.metadata} />
);

/** Lines to show and highlight, as a citation asks. A new nonce asks again. */
export interface LineFocus {
  start: number;
  end: number;
  side: Side;
  nonce: number;
}

const FOCUS_FRAMES = 30;

function FileDiffView({
  path,
  content,
  diffStyle,
  inline,
  composer,
  commenting,
  agentOn,
  expandAll,
  focus,
  onExpandAll,
  onFocused,
}: {
  path: string;
  content: PrFileContent;
  diffStyle: DiffStyle;
  inline: readonly ReviewThread[];
  composer: Composer | null;
  commenting: boolean;
  agentOn: boolean;
  expandAll: boolean;
  focus: LineFocus | null;
  onExpandAll(): void;
  onFocused(): void;
}) {
  const actions = usePrCommentActions();
  const input = useMemo(() => diffInput(content), [content]);
  const hover = useMemo(createHoverStore, []);
  const wrapper = useRef<HTMLDivElement>(null);
  // The diff keeps a line selection of its own and pins the + to its end. Clear it after
  // a pick that opens no line composer here, and when this file's composer closes, so
  // the + follows the pointer again.
  const [clearSelection, setClearSelection] = useState(false);
  const [picks, setPicks] = useState(0);
  const awaitingPick = useRef(false);
  const previousComposer = useRef(composer);
  useEffect(() => {
    const openedHere = composer !== null && composerAnchor(composer).line > 0;
    const closed = previousComposer.current !== null && composer === null;
    previousComposer.current = composer;
    if (closed || (awaitingPick.current && !openedHere)) setClearSelection(true);
    awaitingPick.current = false;
  }, [composer, picks]);
  useEffect(() => {
    if (clearSelection) setClearSelection(false);
  }, [clearSelection]);
  // A + drag shows its range as the diff's selection. Setting the selection makes the
  // diff report it through onLineSelected, which must not pick lines mid-drag.
  const [dragRange, setDragRange] = useState<SelectedLineRange | null>(null);
  const dragging = useRef(false);
  useEffect(() => {
    if (dragRange === null) dragging.current = false;
  }, [dragRange]);
  const onDrag = useStableCallback((range: SelectedLineRange | null) => {
    if (range !== null) dragging.current = true;
    setDragRange(range);
  });
  // A citation selects its lines the same way, then scrolls the first one into view. Lines
  // outside the hunks need the unchanged lines expanded first.
  const [focusRange, setFocusRange] = useState<SelectedLineRange | null>(null);
  const focusing = useRef(false);
  const handledFocus = useRef<number | null>(null);
  useEffect(() => {
    if (focus === null || handledFocus.current === focus.nonce) return;
    handledFocus.current = focus.nonce;
    if (!expandAll && !inHunks(content.commentable, focus.side, focus.start)) onExpandAll();
    focusing.current = true;
    setFocusRange({ start: focus.start, end: focus.end, side: focus.side, endSide: focus.side });
  }, [focus, expandAll, content.commentable, onExpandAll]);
  useEffect(() => {
    if (focusRange === null) {
      focusing.current = false;
      return;
    }
    let frames = 0;
    let frame = 0;
    const reveal = () => {
      const host = wrapper.current?.querySelector("diffs-container");
      const line = host?.shadowRoot?.querySelector("[data-selected-line]") ?? null;
      if (line === null && frames++ < FOCUS_FRAMES) {
        frame = requestAnimationFrame(reveal);
        return;
      }
      (line ?? wrapper.current)?.scrollIntoView({ behavior: "smooth", block: "center" });
      setFocusRange(null);
      onFocused();
    };
    frame = requestAnimationFrame(reveal);
    return () => cancelAnimationFrame(frame);
  }, [focusRange, onFocused]);
  const onRange = useStableCallback((range: SelectedLineRange) => {
    actions.pickLines(path, range, content.commentable);
    awaitingPick.current = true;
    setPicks((count) => count + 1);
  });
  const options = useMemo<FileDiffOptions<AnnotationData, undefined>>(
    () => ({
      diffStyle,
      disableFileHeader: true,
      expandUnchanged: expandAll,
      ...(commenting
        ? {
            enableGutterUtility: true,
            enableLineSelection: true,
            onLineSelected: (range: SelectedLineRange | null) => {
              if (range !== null && !dragging.current && !focusing.current) onRange(range);
            },
            onLineEnter: ({ lineNumber, annotationSide }: { lineNumber: number; annotationSide: Side }) =>
              hover.enter({ lineNumber, side: annotationSide }),
          }
        : {}),
    }),
    [diffStyle, expandAll, commenting, onRange, hover],
  );
  const lineAnnotations = useMemo(
    () => reviewAnnotations(inline, composer === null ? null : composerAnchor(composer)),
    [inline, composer],
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
    <div ref={wrapper}>
      <MultiFileDiff
        {...input}
        options={options}
        selectedLines={clearSelection ? null : (dragRange ?? focusRange ?? undefined)}
        lineAnnotations={lineAnnotations}
        renderAnnotation={renderReviewAnnotation}
        renderGutterUtility={
          commenting
            ? () => (
                <GutterPlus
                  hover={hover}
                  commentable={content.commentable}
                  agentOn={agentOn}
                  onDrag={onDrag}
                  onPick={onRange}
                />
              )
            : undefined
        }
      />
    </div>
  );
}

const NO_THREADS: readonly ReviewThread[] = [];

function FileBody({
  file,
  load,
  diffStyle,
  placed,
  composer,
  commenting,
  agentOn,
  focus,
  onFocused,
  onRetry,
}: {
  file: PrFile;
  load: FileLoad | undefined;
  diffStyle: DiffStyle;
  placed: PlacedThreads | undefined;
  composer: Composer | null;
  commenting: boolean;
  agentOn: boolean;
  focus: LineFocus | null;
  onFocused(): void;
  onRetry(): void;
}) {
  const [expandAll, setExpandAll] = useState(false);
  const onExpandAll = useCallback(() => setExpandAll(true), []);
  const commentable = load?.state === "loaded" ? load.content.commentable : undefined;
  const { shown, hidden } = useMemo(() => {
    const inline = placed?.inline ?? NO_THREADS;
    return expandAll ? { shown: [...inline], hidden: [] } : splitByHunks(inline, commentable);
  }, [placed, commentable, expandAll]);
  const above = useMemo(() => [...hidden, ...(placed?.block ?? NO_THREADS)], [hidden, placed]);
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
        inline={shown}
        composer={composer}
        commenting={commenting}
        agentOn={agentOn}
        expandAll={expandAll}
        focus={focus}
        onExpandAll={onExpandAll}
        onFocused={onFocused}
      />
    );
  }
  return (
    <>
      <FileThreadsBlock threads={above} composer={composer} />
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
  composer: Composer | null;
  commenting: boolean;
  agentOn: boolean;
  /** Lines a citation asks to show in this file, else null. */
  focus: LineFocus | null;
  onFocused(): void;
  onLoad(path: string, head: string, force?: boolean): void;
  onToggleCollapsed(file: PrFile): void;
  onToggleViewed(path: string, viewed: boolean): void;
}

const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? "" : "s"}`;

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
  agentOn,
  focus,
  onFocused,
  onLoad,
  onToggleCollapsed,
  onToggleViewed,
}: PrFileCardProps) {
  const actions = usePrCommentActions();
  const ref = useRef<HTMLElement>(null);
  const near = useNearViewport(ref, scrollRoot, "600px");
  const wantsContent = near && !collapsed && !file.binary;
  const all = [...(placed?.inline ?? []), ...(placed?.block ?? [])];
  const comments = all.filter((t) => t.kind === "review_comment").length;
  const questions = all.filter((t) => t.kind === "question").length;

  useEffect(() => {
    if (wantsContent && load === undefined) onLoad(file.path, head);
  }, [wantsContent, load, file.path, head, onLoad]);

  const open = (mode: ComposerMode) => {
    if (collapsed) onToggleCollapsed(file);
    actions.openComposer(fileComposer(file.path, mode));
  };

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
        {comments > 0 && (
          <span className="tag" title="Review comments on this file">
            {plural(comments, "comment")}
          </span>
        )}
        {questions > 0 && (
          <span className="tag tag-agent" title="Questions to the agent about this file">
            {plural(questions, "question")}
          </span>
        )}
        <span className="spacer" />
        {commenting && agentOn && (
          <button type="button" className="button button-small ask-button" onClick={() => open("question")}>
            Ask about this file
          </button>
        )}
        {commenting && (
          <button type="button" className="button button-small" onClick={() => open("comment")}>
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
          agentOn={agentOn}
          focus={focus}
          onFocused={onFocused}
          onRetry={() => onLoad(file.path, head, true)}
        />
      )}
    </section>
  );
});
