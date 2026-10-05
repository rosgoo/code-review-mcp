import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

const components: Components = {
  a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer noopener" />,
};

const plugins = [remarkGfm];

/** Render markdown as React elements. Raw HTML in the source is dropped. */
export function Markdown({ text, className = "" }: { text: string; className?: string }) {
  return (
    <div className={`markdown ${className}`}>
      <ReactMarkdown remarkPlugins={plugins} components={components} skipHtml>
        {text}
      </ReactMarkdown>
    </div>
  );
}
