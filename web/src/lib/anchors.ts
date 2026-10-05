import type { FileDiffMetadata, SelectedLineRange } from "@pierre/diffs";
import { describeLine } from "./patch";
import type { NewComment, Side } from "./types";

const MAX_CONTENT_LINES = 200;

export interface CommentAnchor {
  path: string;
  side: Side;
  line: number;
  startLine?: number;
  startSide?: Side;
  lineContent: string;
}

interface Endpoint {
  side: Side;
  line: number;
  text: string;
}

function endpoint(
  fileDiff: FileDiffMetadata,
  side: Side,
  line: number,
): Endpoint | null {
  const info = describeLine(fileDiff, side, line);
  if (info === null) return null;
  if (side === "deletions" && !info.changed && info.additionsLine !== null) {
    return { side: "additions", line: info.additionsLine, text: info.text };
  }
  return { side, line, text: info.text };
}

/**
 * Turn a selection in a diff into a comment anchor. An unchanged line selected on
 * the deletions side moves to its additions-side number, so a context line always
 * carries the line number of the new file.
 */
export function anchorFromDiffSelection(
  path: string,
  fileDiff: FileDiffMetadata,
  range: SelectedLineRange,
): CommentAnchor | null {
  const startSide = range.side ?? "additions";
  let start = endpoint(fileDiff, startSide, range.start);
  let end = endpoint(fileDiff, range.endSide ?? startSide, range.end);
  if (start === null || end === null) return null;
  if (start.side !== end.side) {
    return {
      path,
      side: end.side,
      line: end.line,
      startLine: start.line,
      startSide: start.side,
      lineContent: end.text,
    };
  }
  if (start.line > end.line) [start, end] = [end, start];
  if (start.line === end.line) {
    return { path, side: end.side, line: end.line, lineContent: end.text };
  }
  const texts: string[] = [];
  for (
    let line = start.line;
    line <= end.line && texts.length < MAX_CONTENT_LINES;
    line++
  ) {
    texts.push(describeLine(fileDiff, end.side, line)?.text ?? "");
  }
  return {
    path,
    side: end.side,
    line: end.line,
    startLine: start.line,
    startSide: start.side,
    lineContent: texts.join("\n"),
  };
}

export function anchorFromFileSelection(
  path: string,
  content: string,
  range: SelectedLineRange,
): CommentAnchor {
  const start = Math.min(range.start, range.end);
  const end = Math.max(range.start, range.end);
  const lineContent = content
    .split("\n")
    .slice(start - 1, Math.min(end, start - 1 + MAX_CONTENT_LINES))
    .join("\n");
  return start === end
    ? { path, side: "additions", line: end, lineContent }
    : {
        path,
        side: "additions",
        line: end,
        startLine: start,
        startSide: "additions",
        lineContent,
      };
}

export function toNewComment(anchor: CommentAnchor, body: string): NewComment {
  const comment: NewComment = {
    path: anchor.path,
    side: anchor.side,
    line: anchor.line,
    line_content: anchor.lineContent,
    body,
  };
  if (anchor.startLine !== undefined) {
    comment.start_line = anchor.startLine;
    comment.start_side = anchor.startSide ?? anchor.side;
  }
  return comment;
}

export function sameAnchor(
  a: CommentAnchor | null,
  b: CommentAnchor | null,
): boolean {
  return (
    a !== null &&
    b !== null &&
    a.path === b.path &&
    a.side === b.side &&
    a.line === b.line &&
    a.startLine === b.startLine &&
    a.startSide === b.startSide
  );
}
