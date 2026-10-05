export interface TreeFile {
  kind: "file";
  name: string;
  path: string;
}

export interface TreeDir {
  kind: "dir";
  name: string;
  children: TreeNode[];
}

export type TreeNode = TreeFile | TreeDir;

const byName = (a: TreeNode, b: TreeNode) =>
  a.kind === b.kind ? a.name.localeCompare(b.name) : a.kind === "dir" ? -1 : 1;

function collapse(node: TreeNode): TreeNode {
  if (node.kind === "file") return node;
  let dir = node;
  while (dir.children.length === 1 && dir.children[0]?.kind === "dir") {
    const only = dir.children[0];
    dir = {
      kind: "dir",
      name: `${dir.name}/${only.name}`,
      children: only.children,
    };
  }
  return { ...dir, children: dir.children.map(collapse).sort(byName) };
}

/** Build a directory tree from file paths, merging chains of single-child directories. */
export function buildFileTree(paths: readonly string[]): TreeNode[] {
  const root: TreeDir = { kind: "dir", name: "", children: [] };
  for (const path of paths) {
    const parts = path.split("/");
    const fileName = parts.pop() ?? path;
    let dir = root;
    for (const part of parts) {
      let next = dir.children.find(
        (c): c is TreeDir => c.kind === "dir" && c.name === part,
      );
      if (next === undefined) {
        next = { kind: "dir", name: part, children: [] };
        dir.children.push(next);
      }
      dir = next;
    }
    dir.children.push({ kind: "file", name: fileName, path });
  }
  return root.children.map(collapse).sort(byName);
}

export function filterPaths(paths: readonly string[], query: string): string[] {
  const needle = query.trim().toLowerCase();
  return needle
    ? paths.filter((p) => p.toLowerCase().includes(needle))
    : [...paths];
}
