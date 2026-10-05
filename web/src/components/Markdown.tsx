import { useMemo } from "react";
import ReactMarkdown, { type Components, type Options } from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  citationHref,
  parseCitationHref,
  splitCitations,
  wholeCitation,
  type Citation,
  type CitationMatch,
} from "../lib/citations";

export interface CitationHandler {
  /** True when the path is a file of the PR, so a link can show it. */
  inPr(path: string): boolean;
  open(citation: Citation): void;
}

interface MdNode {
  type: string;
  value?: string;
  url?: string;
  children?: MdNode[];
}

const linkNode = (match: CitationMatch, code: boolean, children: MdNode[]): MdNode => ({
  type: "link",
  url: citationHref(match, code),
  children,
});

/** Turn `path:line` citations in text and in whole inline code into links. Code blocks and
 * existing links keep their text. */
function linkCitations(node: MdNode): void {
  if (node.children === undefined || node.type === "link" || node.type === "linkReference") return;
  const children: MdNode[] = [];
  for (const child of node.children) {
    if (child.type === "text" && child.value) {
      for (const segment of splitCitations(child.value)) {
        children.push(
          segment.kind === "text"
            ? { type: "text", value: segment.text }
            : linkNode(segment, false, [{ type: "text", value: segment.text }]),
        );
      }
    } else if (child.type === "inlineCode" && child.value) {
      const match = wholeCitation(child.value);
      children.push(match === null ? child : linkNode(match, true, [child]));
    } else {
      if (child.type !== "code") linkCitations(child);
      children.push(child);
    }
  }
  node.children = children;
}

const remarkCitations = () => (tree: unknown) => linkCitations(tree as MdNode);

const baseComponents: Components = {
  a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer noopener" />,
};

type Plugins = NonNullable<Options["remarkPlugins"]>;
const plugins: Plugins = [remarkGfm];
const citingPlugins: Plugins = [remarkGfm, remarkCitations];

function citationComponents(handler: CitationHandler): Components {
  return {
    a: ({ node: _node, href, children, ...props }) => {
      const citation = parseCitationHref(href);
      if (citation === null) {
        return (
          <a {...props} href={href} target="_blank" rel="noreferrer noopener">
            {children}
          </a>
        );
      }
      if (!handler.inPr(citation.path)) {
        return citation.code ? (
          <>{children}</>
        ) : (
          <code className="citation-outside">{children}</code>
        );
      }
      return (
        <a
          href={href}
          className="citation"
          title={`Show ${citation.path} line ${citation.start}${citation.end !== citation.start ? `–${citation.end}` : ""}`}
          onClick={(event) => {
            event.preventDefault();
            handler.open(citation);
          }}
        >
          {children}
        </a>
      );
    },
  };
}

/**
 * Render markdown as React elements. Raw HTML in the source is dropped. With `citations`,
 * `path:line` and `path:start-end` become links to files of the PR.
 */
export function Markdown({
  text,
  className = "",
  citations,
}: {
  text: string;
  className?: string;
  citations?: CitationHandler;
}) {
  const components = useMemo(
    () => (citations === undefined ? baseComponents : citationComponents(citations)),
    [citations],
  );
  return (
    <div className={`markdown ${className}`}>
      <ReactMarkdown
        remarkPlugins={citations === undefined ? plugins : citingPlugins}
        components={components}
        skipHtml
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
