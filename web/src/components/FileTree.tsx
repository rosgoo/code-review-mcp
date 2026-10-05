import { useMemo, useState } from "react";
import type { ChangeCounts } from "../lib/patch";
import { buildFileTree, filterPaths, type TreeDir, type TreeNode } from "../lib/tree";

interface TreeProps {
  counts: ReadonlyMap<string, ChangeCounts>;
  onSelect(path: string): void;
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

function TreeList({ nodes, depth, ...props }: TreeProps & { nodes: TreeNode[]; depth: number }) {
  return (
    <ul>
      {nodes.map((node) =>
        node.kind === "dir" ? (
          <TreeDirItem key={`dir:${node.name}`} node={node} depth={depth} {...props} />
        ) : (
          <li key={node.path}>
            <button
              type="button"
              className="tree-file"
              style={indent(depth)}
              title={node.path}
              onClick={() => props.onSelect(node.path)}
            >
              <span className="tree-file-name">{node.name}</span>
              <Counts counts={props.counts.get(node.path)} />
            </button>
          </li>
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
