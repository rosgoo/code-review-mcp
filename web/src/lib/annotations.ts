import type { DiffLineAnnotation, LineAnnotation } from "@pierre/diffs";
import type { CommentAnchor } from "./anchors";
import type { Comment, Side } from "./types";

export interface AnnotationData {
  threadIds: string[];
  composer: boolean;
}

export const commentSide = (comment: Comment): Side =>
  comment.line_type === "delete" ? "deletions" : "additions";

/**
 * Group the threads of `path` into one annotation per (side, line), so several
 * threads on a line share one slot. The open composer joins the group of its line.
 */
export function diffAnnotations(
  comments: readonly Comment[],
  path: string,
  composer: CommentAnchor | null,
): DiffLineAnnotation<AnnotationData>[] {
  const groups = new Map<string, DiffLineAnnotation<AnnotationData>>();
  const groupFor = (side: Side, lineNumber: number) => {
    const key = `${side}:${lineNumber}`;
    let group = groups.get(key);
    if (group === undefined) {
      group = {
        side,
        lineNumber,
        metadata: { threadIds: [], composer: false },
      };
      groups.set(key, group);
    }
    return group;
  };
  for (const comment of comments) {
    if (comment.file_path === path) {
      groupFor(
        commentSide(comment),
        comment.line_number,
      ).metadata.threadIds.push(comment.id);
    }
  }
  if (composer?.path === path)
    groupFor(composer.side, composer.line).metadata.composer = true;
  return [...groups.values()];
}

export function fileAnnotations(
  comments: readonly Comment[],
  path: string,
  composer: CommentAnchor | null,
): LineAnnotation<AnnotationData>[] {
  const byLine = new Map<number, LineAnnotation<AnnotationData>>();
  for (const { lineNumber, metadata } of diffAnnotations(
    comments,
    path,
    composer,
  )) {
    const group = byLine.get(lineNumber);
    if (group === undefined) {
      byLine.set(lineNumber, { lineNumber, metadata });
    } else {
      group.metadata.threadIds.push(...metadata.threadIds);
      group.metadata.composer ||= metadata.composer;
    }
  }
  return [...byLine.values()];
}
