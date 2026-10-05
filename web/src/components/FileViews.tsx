import {
  parseDiffFromFile,
  type DiffLineAnnotation,
  type FileContents,
  type LineAnnotation,
  type SelectedLineRange,
} from "@pierre/diffs";
import {
  File,
  FileDiff,
  MultiFileDiff,
  useStableCallback,
  type FileDiffOptions,
  type FileOptions,
} from "@pierre/diffs/react";
import { useMemo, useState } from "react";
import {
  anchorFromDiffSelection,
  anchorFromFileSelection,
  type CommentAnchor,
} from "../lib/anchors";
import { diffAnnotations, fileAnnotations, type AnnotationData } from "../lib/annotations";
import type { FileEntry } from "../lib/entries";
import type { Comment, DiffStyle, ViewFile } from "../lib/types";
import { Markdown } from "./Markdown";
import { AnnotationSlot } from "./Thread";


export const fileDomId = (path: string) => `file-${path}`;

interface CommentTargetProps {
  comments: readonly Comment[];
  composer: CommentAnchor | null;
  onAnchor(anchor: CommentAnchor): void;
}

function useCommentInteractions(onRange: (range: SelectedLineRange) => void) {
  const stableOnRange = useStableCallback(onRange);
  return useMemo(
    () => ({
      enableGutterUtility: true,
      onGutterUtilityClick: stableOnRange,
      enableLineSelection: true,
      onLineSelected: (range: SelectedLineRange | null) => {
        if (range !== null) stableOnRange(range);
      },
    }),
    [stableOnRange],
  );
}

function CollapseButton({ collapsed, onToggle }: { collapsed: boolean; onToggle(): void }) {
  return (
    <button
      type="button"
      className="collapse-button"
      aria-label={collapsed ? "Expand file" : "Collapse file"}
      onClick={onToggle}
    >
      {collapsed ? "▸" : "▾"}
    </button>
  );
}

const renderDiffAnnotation = (annotation: DiffLineAnnotation<AnnotationData>) => (
  <AnnotationSlot data={annotation.metadata} />
);

const renderFileAnnotation = (annotation: LineAnnotation<AnnotationData>) => (
  <AnnotationSlot data={annotation.metadata} />
);

type DiffEntry = Exclude<FileEntry, { kind: "plain" }>;

export function DiffEntryView({
  entry,
  diffStyle,
  comments,
  composer,
  onAnchor,
}: CommentTargetProps & { entry: DiffEntry; diffStyle: DiffStyle }) {
  const [collapsed, setCollapsed] = useState(false);
  const fileDiff = useMemo(
    () =>
      entry.kind === "patch" ? entry.fileDiff : parseDiffFromFile(entry.oldFile, entry.newFile),
    [entry],
  );
  const interactions = useCommentInteractions((range) => {
    const anchor = anchorFromDiffSelection(entry.path, fileDiff, range);
    if (anchor !== null) onAnchor(anchor);
  });
  const options = useMemo<FileDiffOptions<AnnotationData, undefined>>(
    () => ({ ...interactions, diffStyle, collapsed }),
    [interactions, diffStyle, collapsed],
  );
  const lineAnnotations = useMemo(
    () => diffAnnotations(comments, entry.path, composer),
    [comments, entry.path, composer],
  );
  const shared = {
    options,
    lineAnnotations,
    renderAnnotation: renderDiffAnnotation,
    renderHeaderPrefix: () => (
      <CollapseButton collapsed={collapsed} onToggle={() => setCollapsed(!collapsed)} />
    ),
  };

  return (
    <section id={fileDomId(entry.path)} className="file-card">
      {entry.kind === "patch" ? (
        <FileDiff fileDiff={entry.fileDiff} {...shared} />
      ) : (
        <MultiFileDiff oldFile={entry.oldFile} newFile={entry.newFile} {...shared} />
      )}
    </section>
  );
}

const hasExtension = (path: string) => /\.[^/.]+$/.test(path.split("/").pop() ?? "");

function languageOverride(file: ViewFile): string | undefined {
  return hasExtension(file.path) || file.language === "plaintext" ? undefined : file.language;
}

type MarkdownView = "rendered" | "source";

export function PlainFileView({
  file,
  comments,
  composer,
  onAnchor,
}: CommentTargetProps & { file: ViewFile }) {
  const [collapsed, setCollapsed] = useState(false);
  const [markdownChoice, setMarkdownChoice] = useState<MarkdownView | null>(null);
  const isMarkdown = file.language === "markdown";
  const hasThreads =
    composer?.path === file.path || comments.some((c) => c.file_path === file.path);
  const markdownView = markdownChoice ?? (hasThreads ? "source" : "rendered");

  const contents = useMemo<FileContents>(
    () => ({ name: file.path, contents: file.content, lang: languageOverride(file) }),
    [file],
  );
  const interactions = useCommentInteractions((range) =>
    onAnchor(anchorFromFileSelection(file.path, file.content, range)),
  );
  const options = useMemo<FileOptions<AnnotationData, undefined>>(
    () => ({ ...interactions, collapsed, disableFileHeader: isMarkdown }),
    [interactions, collapsed, isMarkdown],
  );
  const lineAnnotations = useMemo(
    () => fileAnnotations(comments, file.path, composer),
    [comments, file.path, composer],
  );

  const source = (
    <File
      file={contents}
      options={options}
      lineAnnotations={lineAnnotations}
      renderAnnotation={renderFileAnnotation}
      renderHeaderPrefix={() => (
        <CollapseButton collapsed={collapsed} onToggle={() => setCollapsed(!collapsed)} />
      )}
    />
  );
  if (!isMarkdown) {
    return (
      <section id={fileDomId(file.path)} className="file-card">
        {source}
      </section>
    );
  }

  return (
    <section id={fileDomId(file.path)} className="file-card">
      <div className="file-toolbar">
        <CollapseButton collapsed={collapsed} onToggle={() => setCollapsed(!collapsed)} />
        <span className="file-toolbar-name">{file.path}</span>
        <span className="spacer" />
        <div className="segmented" role="group" aria-label="Markdown view">
          {(["rendered", "source"] as const).map((view) => (
            <button
              key={view}
              type="button"
              className={view === markdownView ? "active" : ""}
              aria-pressed={view === markdownView}
              title={view === "source" ? "Comment on lines in the source view" : undefined}
              onClick={() => setMarkdownChoice(view)}
            >
              {view === "rendered" ? "Rendered" : "Source"}
            </button>
          ))}
        </div>
      </div>
      {collapsed ? null : markdownView === "rendered" ? (
        <Markdown text={file.content} className="markdown-file" />
      ) : (
        source
      )}
    </section>
  );
}
