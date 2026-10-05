import { useMemo, useState } from "react";
import type { ChangeCounts } from "../lib/patch";
import { statusLetter } from "../lib/pr";
import { buildFileTree, filterPaths, type TreeDir, type TreeFile, type TreeNode } from "../lib/tree";
import type { FileStatus } from "../lib/types";

export interface FileDetail {
  status: FileStatus;
  oldPath: string | null;
  viewed: boolean;
}

interface TreeProps {
  counts: ReadonlyMap<string, ChangeCounts>;
  details?: ReadonlyMap<string, FileDetail>;
  onSelect(path: string): void;
  onToggleViewed?(path: string, viewed: boolean): void;
}

const indent = (depth: number) => ({ paddingLeft: `${8 + depth * 14}px` });

function Counts({ counts }: { counts: ChangeCounts | undefined }) {
  if (counts === undefined) return null;
  return (
    <span className="tree-counts">
      {counts.additions > 0 && <span className="count-add">+{counts.additions}</span>}
      {counts.deletions > 0 && <span className="count-del">−{counts.deletions}</span>}
    </span>
  );
}

function TreeDirItem({ node, depth, ...props }: TreeProps & { node: TreeDir; depth: number }) {
  const [open, setOpen] = useState(true);
  return (
    <li>
      <button
        type="button"
        className="tree-dir"
        style={indent(depth)}
        title={node.name}
        onClick={() => setOpen(!open)}
      >
        <span className="tree-caret">{open ? "▾" : "▸"}</span>
        {node.name}
      </button>
      {open && <TreeList nodes={node.children} depth={depth + 1} {...props} />}
    </li>
  );
}

function TreeFileItem({
  node,
  depth,
  counts,
  details,
  onSelect,
  onToggleViewed,
}: TreeProps & { node: TreeFile; depth: number }) {
  const detail = details?.get(node.path);
  const title = detail?.oldPath ? `${detail.oldPath} → ${node.path}` : node.path;
  return (
    <li className={detail?.viewed ? "tree-row viewed" : "tree-row"}>
      <button
        type="button"
        className="tree-file"
        style={indent(depth)}
        title={title}
        onClick={() => onSelect(node.path)}
      >
        {detail && (
          <span className={`status-letter status-${detail.status}`} title={detail.status}>
            {statusLetter(detail.status)}
          </span>
        )}
        <span className="tree-file-name">
          {node.name}
          {detail?.oldPath && <span className="tree-old-path">← {detail.oldPath}</span>}
        </span>
        <Counts counts={counts.get(node.path)} />
      </button>
      {detail && onToggleViewed && (
        <input
          type="checkbox"
          className="tree-viewed"
          checked={detail.viewed}
          title={detail.viewed ? "Viewed" : "Mark as viewed"}
          aria-label={`Viewed ${node.path}`}
          onChange={(event) => onToggleViewed(node.path, event.target.checked)}
        />
      )}
    </li>
  );
}

function TreeList({ nodes, depth, ...props }: TreeProps & { nodes: TreeNode[]; depth: number }) {
  return (
    <ul>
      {nodes.map((node) =>
        node.kind === "dir" ? (
          <TreeDirItem key={`dir:${node.name}`} node={node} depth={depth} {...props} />
        ) : (
          <TreeFileItem key={node.path} node={node} depth={depth} {...props} />
        ),
      )}
    </ul>
  );
}

export function FileTree({ paths, ...props }: TreeProps & { paths: readonly string[] }) {
  const [query, setQuery] = useState("");
  const tree = useMemo(() => buildFileTree(filterPaths(paths, query)), [paths, query]);
  return (
    <nav className="file-tree" aria-label="Files">
      <input
        type="search"
        className="tree-filter"
        placeholder="Filter files…"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      />
      {tree.length === 0 ? (
        <p className="muted tree-empty">No files</p>
      ) : (
        <TreeList nodes={tree} depth={0} {...props} />
      )}
    </nav>
  );
}
