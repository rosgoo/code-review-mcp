import type { DiffFileInput } from "@pierre/diffs";
import { MultiFileDiff, type FileDiffOptions } from "@pierre/diffs/react";
import { memo, useEffect, useMemo, useRef, type ReactNode } from "react";
import { useNearViewport } from "../lib/hooks";
import { estimatedDiffHeight, statusLetter } from "../lib/pr";
import type { DiffStyle, PrFile, PrFileContent } from "../lib/types";
import { fileDomId } from "./FileViews";

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
    <div className="file-placeholder" style={height === undefined ? undefined : { minHeight: height }}>
      {children}
    </div>
  );
}

function FileBody({
  file,
  load,
  diffStyle,
  onRetry,
}: {
  file: PrFile;
  load: FileLoad | undefined;
  diffStyle: DiffStyle;
  onRetry(): void;
}) {
  const content = load?.state === "loaded" ? load.content : null;
  const input = useMemo(() => (content === null ? null : diffInput(content)), [content]);
  const options = useMemo<FileDiffOptions<undefined, undefined>>(
    () => ({ diffStyle, disableFileHeader: true }),
    [diffStyle],
  );

  if (file.binary) return <Placeholder>Binary file not shown.</Placeholder>;
  if (load === undefined || load.state === "loading") {
    return <Placeholder height={estimatedDiffHeight(file)}>Loading…</Placeholder>;
  }
  if (load.state === "error") {
    return (
      <Placeholder>
        <span className="form-error">{load.message}</span>{" "}
        <button type="button" className="link-button" onClick={onRetry}>
          Retry
        </button>
      </Placeholder>
    );
  }
  if (load.content.binary) return <Placeholder>Binary file not shown.</Placeholder>;
  if (load.content.too_large) {
    return <Placeholder>This file is too large to show here. Open it on GitHub.</Placeholder>;
  }
  if (input === null) return <Placeholder>No content.</Placeholder>;
  if (
    input.oldFile !== null &&
    input.newFile !== null &&
    input.oldFile.contents === input.newFile.contents
  ) {
    return <Placeholder>No content changes.</Placeholder>;
  }
  return <MultiFileDiff {...input} options={options} />;
}

interface PrFileCardProps {
  file: PrFile;
  head: string;
  load: FileLoad | undefined;
  collapsed: boolean;
  diffStyle: DiffStyle;
  scrollRoot: Element | null;
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
  onLoad,
  onToggleCollapsed,
  onToggleViewed,
}: PrFileCardProps) {
  const ref = useRef<HTMLElement>(null);
  const near = useNearViewport(ref, scrollRoot, "600px");
  const wantsContent = near && !collapsed && !file.binary;

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
        <span className="spacer" />
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
          onRetry={() => onLoad(file.path, head, true)}
        />
      )}
    </section>
  );
});
