import type { FileContents, FileDiffMetadata } from "@pierre/diffs";
import { changeCounts, splitPatch, type ChangeCounts } from "./patch";
import type { ReviewView, ViewFile } from "./types";

export type FileEntry =
  | { kind: "patch"; path: string; counts: ChangeCounts; fileDiff: FileDiffMetadata }
  | {
      kind: "full";
      path: string;
      counts: ChangeCounts;
      oldFile: FileContents;
      newFile: FileContents;
    }
  | { kind: "plain"; path: string; file: ViewFile };

/**
 * Decide how each file of a review renders. An annotated review (files plus a diff)
 * shows full old/new contents when the backend rebuilt the old side, and falls back
 * to the file's patch otherwise; files only in the patch, such as deleted ones,
 * render from the patch.
 */
export function fileEntries(view: ReviewView): FileEntry[] {
  if (view.mode === "empty") return [];
  if (view.mode === "diff") {
    return splitPatch(view.diff).map(({ path, fileDiff }) => ({
      kind: "patch",
      path,
      counts: changeCounts(fileDiff),
      fileDiff,
    }));
  }
  if (view.diff === undefined) {
    return view.files.map((file) => ({ kind: "plain", path: file.path, file }));
  }
  const stored = new Map(view.files.map((file) => [file.path, file]));
  const entries: FileEntry[] = splitPatch(view.diff).map(({ path, fileDiff }) => {
    const file = stored.get(path);
    const counts = changeCounts(fileDiff);
    stored.delete(path);
    if (typeof file?.old_content === "string") {
      return {
        kind: "full",
        path,
        counts,
        oldFile: { name: fileDiff.prevName ?? path, contents: file.old_content },
        newFile: { name: path, contents: file.content },
      };
    }
    return { kind: "patch", path, counts, fileDiff };
  });
  for (const file of stored.values()) entries.push({ kind: "plain", path: file.path, file });
  return entries;
}
