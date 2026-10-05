export interface Citation {
  path: string;
  start: number;
  end: number;
}

export interface CitationMatch extends Citation {
  /** The cited text as written, for example `src/a.py:3-5`. */
  text: string;
  index: number;
}

export type CitationSegment = { kind: "text"; text: string } | ({ kind: "cite" } & CitationMatch);

// A path, then :line or :start-end. The lookbehind keeps a match from starting inside a
// URL, a longer path, or a word; the lookahead keeps it from ending inside one.
const CITATION =
  /(?<![\w./@:#-])((?:\.\/)?[\w.-]+(?:\/[\w.-]+)*):(\d{1,6})(?:\s?[-–]\s?(\d{1,6}))?(?![\w/])/g;

const EXTENSION = /\.[A-Za-z][\w-]*$/;

/** A file path names a directory or has an extension: `10:30` and `localhost:80` do not. */
const isPathLike = (path: string) =>
  path.includes("/") ? !path.split("/").includes("..") : EXTENSION.test(path);

export function findCitations(text: string): CitationMatch[] {
  const found: CitationMatch[] = [];
  for (const match of text.matchAll(CITATION)) {
    const [whole, rawPath = "", first = "", last] = match;
    const path = rawPath.replace(/^\.\//, "");
    if (!isPathLike(path)) continue;
    const a = Number(first);
    const b = last === undefined ? a : Number(last);
    if (a < 1 || b < 1) continue;
    found.push({
      path,
      start: Math.min(a, b),
      end: Math.max(a, b),
      text: whole,
      index: match.index,
    });
  }
  return found;
}

export function splitCitations(text: string): CitationSegment[] {
  const segments: CitationSegment[] = [];
  let at = 0;
  for (const match of findCitations(text)) {
    if (match.index > at) segments.push({ kind: "text", text: text.slice(at, match.index) });
    segments.push({ kind: "cite", ...match });
    at = match.index + match.text.length;
  }
  if (at < text.length) segments.push({ kind: "text", text: text.slice(at) });
  return segments;
}

/** The citation when `text` is one citation and nothing else, as inline code often is. */
export function wholeCitation(text: string): CitationMatch | null {
  const trimmed = text.trim();
  const [match] = findCitations(trimmed);
  return match !== undefined && match.text === trimmed ? match : null;
}

export const citationLabel = (c: Citation) =>
  c.start === c.end ? `${c.path}:${c.start}` : `${c.path}:${c.start}-${c.end}`;

const HREF_PREFIX = "#cite:";

/** Citation links carry the target in a fragment, which markdown URL sanitizing keeps. */
export const citationHref = (c: Citation, code: boolean) =>
  `${HREF_PREFIX}${code ? "c" : "t"}:${c.start}:${c.end}:${encodeURIComponent(c.path)}`;

export function parseCitationHref(href: string | undefined): (Citation & { code: boolean }) | null {
  if (href === undefined || !href.startsWith(HREF_PREFIX)) return null;
  const [kind, start, end, ...rest] = href.slice(HREF_PREFIX.length).split(":");
  const path = decodeURIComponent(rest.join(":"));
  if ((kind !== "c" && kind !== "t") || !path) return null;
  return { path, start: Number(start), end: Number(end), code: kind === "c" };
}
