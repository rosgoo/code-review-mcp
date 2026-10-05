import { useState, type KeyboardEvent } from "react";
import { Markdown } from "./Markdown";
import { errorMessage } from "./Thread";

interface MarkdownEditorProps {
  initial?: string;
  placeholder: string;
  submitLabel: string;
  onSubmit(text: string): Promise<void>;
  /** A second way to submit, shown before the main button. */
  secondary?: { label: string; onSubmit(text: string): Promise<void> };
  onCancel(): void;
}

/**
 * A textarea with Write and Preview tabs. ⌘/Ctrl+Enter submits with the main action and
 * Escape cancels.
 */
export function MarkdownEditor({
  initial = "",
  placeholder,
  submitLabel,
  onSubmit,
  secondary,
  onCancel,
}: MarkdownEditorProps) {
  const [text, setText] = useState(initial);
  const [tab, setTab] = useState<"write" | "preview">("write");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(action: (text: string) => Promise<void> = onSubmit) {
    const body = text.trim();
    if (!body || busy) return;
    setBusy(true);
    setError(null);
    try {
      await action(body);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLElement>) {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      void submit();
    } else if (event.key === "Escape") {
      event.preventDefault();
      onCancel();
    }
  }

  return (
    <div className="md-editor" onKeyDown={onKeyDown}>
      <div className="md-tabs" role="tablist">
        {(["write", "preview"] as const).map((name) => (
          <button
            key={name}
            type="button"
            role="tab"
            aria-selected={tab === name}
            className={tab === name ? "active" : ""}
            onClick={() => setTab(name)}
          >
            {name === "write" ? "Write" : "Preview"}
          </button>
        ))}
      </div>
      {tab === "write" ? (
        <textarea
          autoFocus
          rows={4}
          value={text}
          placeholder={placeholder}
          disabled={busy}
          onChange={(event) => setText(event.target.value)}
        />
      ) : (
        <div className="md-preview">
          {text.trim() ? <Markdown text={text} /> : <p className="muted">Nothing to preview.</p>}
        </div>
      )}
      {error && <p className="form-error">{error}</p>}
      <div className="form-actions">
        <span className="form-hint">Markdown · ⌘/Ctrl + Enter</span>
        <button type="button" className="button" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        {secondary && (
          <button
            type="button"
            className="button"
            onClick={() => void submit(secondary.onSubmit)}
            disabled={busy || !text.trim()}
          >
            {secondary.label}
          </button>
        )}
        <button
          type="button"
          className="button primary"
          onClick={() => void submit()}
          disabled={busy || !text.trim()}
        >
          {busy ? "Saving…" : submitLabel}
        </button>
      </div>
    </div>
  );
}
