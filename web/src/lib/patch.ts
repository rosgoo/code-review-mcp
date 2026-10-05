import { parsePatchFiles, type FileDiffMetadata } from "@pierre/diffs";
import type { Side } from "./types";

export interface PatchFile {
  path: string;
  fileDiff: FileDiffMetadata;
}

export function splitPatch(patch: string): PatchFile[] {
  if (!patch.trim()) return [];
  return parsePatchFiles(patch)
    .flatMap((parsed) => parsed.files)
    .map((fileDiff) => ({ path: fileDiff.name, fileDiff }));
}

export interface ChangeCounts {
  additions: number;
  deletions: number;
}

export function changeCounts(fileDiff: FileDiffMetadata): ChangeCounts {
  return fileDiff.hunks.reduce(
    (counts, hunk) => ({
      additions: counts.additions + hunk.additionLines,
      deletions: counts.deletions + hunk.deletionLines,
    }),
    { additions: 0, deletions: 0 },
  );
}

export interface DiffLine {
  text: string;
  changed: boolean;
  /** The same line's number on the additions side, or null for a deleted line. */
  additionsLine: number | null;
}

const stripEol = (line: string | undefined) => (line ?? "").replace(/\r?\n$/, "");

function unchangedLine(
  fileDiff: FileDiffMetadata,
  side: Side,
  lineNumber: number,
  delta: number,
): DiffLine {
  const additionsLine = side === "additions" ? lineNumber : lineNumber + delta;
  const text = fileDiff.isPartial ? "" : stripEol(fileDiff.additionLines[additionsLine - 1]);
  return { text, changed: false, additionsLine };
}

/**
 * Find a line of `fileDiff` by its number on `side`. Unchanged lines between hunks
 * have no text when the diff came from a patch, which holds only the hunk lines.
 */
export function describeLine(
  fileDiff: FileDiffMetadata,
  side: Side,
  lineNumber: number,
): DiffLine | null {
  let delta = 0;
  for (const hunk of fileDiff.hunks) {
    let oldLine = hunk.deletionCount > 0 ? hunk.deletionStart : hunk.deletionStart + 1;
    let newLine = hunk.additionCount > 0 ? hunk.additionStart : hunk.additionStart + 1;
    if (lineNumber < (side === "additions" ? newLine : oldLine)) {
      return unchangedLine(fileDiff, side, lineNumber, delta);
    }
    for (const block of hunk.hunkContent) {
      if (block.type === "context") {
        const offset = lineNumber - (side === "additions" ? newLine : oldLine);
        if (offset >= 0 && offset < block.lines) {
          return {
            text: stripEol(fileDiff.additionLines[block.additionLineIndex + offset]),
            changed: false,
            additionsLine: newLine + offset,
          };
        }
        oldLine += block.lines;
        newLine += block.lines;
      } else {
        if (
          side === "deletions" &&
          lineNumber >= oldLine &&
          lineNumber < oldLine + block.deletions
        ) {
          const text = fileDiff.deletionLines[block.deletionLineIndex + lineNumber - oldLine];
          return { text: stripEol(text), changed: true, additionsLine: null };
        }
        if (
          side === "additions" &&
          lineNumber >= newLine &&
          lineNumber < newLine + block.additions
        ) {
          const text = fileDiff.additionLines[block.additionLineIndex + lineNumber - newLine];
          return {
            text: stripEol(text),
            changed: true,
            additionsLine: lineNumber,
          };
        }
        oldLine += block.deletions;
        newLine += block.additions;
      }
    }
    delta = newLine - oldLine;
  }
  if (fileDiff.type === "deleted" && side === "additions") return null;
  if (fileDiff.type === "new" && side === "deletions") return null;
  return unchangedLine(fileDiff, side, lineNumber, delta);
}
