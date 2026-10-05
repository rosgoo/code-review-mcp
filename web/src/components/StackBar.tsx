import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { reviewPath } from "../lib/router";
import {
  byPosition,
  nextEdge,
  stackKeyMove,
  stackLabel,
  stackPlace,
  stackPrLabel,
  stepTip,
} from "../lib/stack";
import type { PrStack, StackPr } from "../lib/types";
import { CiIcon, StackIcon } from "./Icons";
import { Link } from "./Link";

function StatusTag({ pr }: { pr: StackPr }) {
  if (pr.is_draft && pr.state === "open") return <span className="tag">Draft</span>;
  return <span className={`tag tag-state-${pr.state}`}>{pr.state}</span>;
}

function MenuItem({
  pr,
  marker,
  note,
  current,
  busy,
  onGo,
}: {
  pr: StackPr;
  marker: ReactNode;
  note?: string;
  current: boolean;
  busy: boolean;
  onGo(pr: StackPr): void;
}) {
  const content = (
    <>
      <span className="stack-item-pos">{marker}</span>
      <CiIcon state={pr.ci_state} />
      <span className="pr-ref">#{pr.number}</span>
      <span className="stack-item-title" title={pr.title}>
        {pr.title}
      </span>
      {note && <span className="muted stack-item-note">{note}</span>}
      <StatusTag pr={pr} />
      {current && <span className="tag tag-current">This PR</span>}
    </>
  );
  const className = current ? "stack-item current" : "stack-item";
  if (current) {
    return (
      <li>
        <span className={className} aria-current="page">
          {content}
        </span>
      </li>
    );
  }
  return (
    <li>
      {pr.review_id ? (
        <Link to={reviewPath(pr.review_id)} className={className} role="menuitem">
          {content}
        </Link>
      ) : (
        <button
          type="button"
          className={className}
          role="menuitem"
          disabled={busy}
          title={`Open ${stackPrLabel(pr)}`}
          onClick={() => onGo(pr)}
        >
          {content}
        </button>
      )}
    </li>
  );
}

function StepButton({
  label,
  target,
  tip,
  busy,
  onGo,
}: {
  label: string;
  target: StackPr | null;
  tip: string;
  busy: boolean;
  onGo(pr: StackPr): void;
}) {
  return (
    <span className="button-wrap" title={tip}>
      <button
        type="button"
        className="button button-small"
        disabled={target === null || busy}
        onClick={() => target !== null && onGo(target)}
      >
        {label}
      </button>
    </span>
  );
}

/**
 * The reviewed PR's place in its stack, with prev/next buttons, the `[` and `]` keys,
 * and a menu of every PR in the stack and every PR based on it.
 */
export function StackBar({
  stack,
  number,
  busy,
  onGo,
}: {
  stack: PrStack;
  number: number;
  busy: boolean;
  onGo(pr: StackPr): void;
}) {
  const place = useMemo(() => stackPlace(stack, number), [stack, number]);
  const entries = useMemo(() => byPosition(stack.entries), [stack]);
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.composedPath()[0];
      const move = stackKeyMove(
        event,
        target instanceof HTMLElement ? target : null,
      );
      if (move === null || document.querySelector('[aria-modal="true"]') !== null) return;
      const goal = move === "prev" ? place.prev : place.next;
      if (goal === null || busy) return;
      event.preventDefault();
      onGo(goal);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [place, busy, onGo]);

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: PointerEvent) => {
      if (!(event.target instanceof Node) || !root.current?.contains(event.target)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const total = entries.length + stack.extensions.length;
  return (
    <div className="stack-bar" ref={root}>
      <StackIcon />
      <span className="stack-label">{stackLabel(stack, place)}</span>
      <StepButton
        label="← prev"
        target={place.prev}
        tip={stepTip(stack, place.prev, "This is the first PR in the stack")}
        busy={busy}
        onGo={onGo}
      />
      <StepButton
        label="next →"
        target={place.next}
        tip={stepTip(stack, place.next, nextEdge(place))}
        busy={busy}
        onGo={onGo}
      />
      <button
        type="button"
        className="button button-small"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        All {total} PRs ▾
      </button>
      <span className="muted stack-keys">
        <kbd>[</kbd> <kbd>]</kbd> move
      </span>
      {open && (
        <div className="stack-menu" role="menu" aria-label="PRs in this stack">
          <ol className="stack-menu-list">
            {entries.map((entry) => (
              <MenuItem
                key={entry.number}
                pr={entry}
                marker={entry.position}
                current={entry.number === number}
                busy={busy}
                onGo={(pr) => {
                  setOpen(false);
                  onGo(pr);
                }}
              />
            ))}
          </ol>
          {stack.extensions.length > 0 && (
            <>
              <div className="stack-menu-heading">Based on the stack, not in it</div>
              <ul className="stack-menu-list">
                {stack.extensions.map((extension) => (
                  <MenuItem
                    key={extension.number}
                    pr={extension}
                    marker="↳"
                    note={`based on #${extension.based_on}, not in the stack`}
                    current={extension.number === number}
                    busy={busy}
                    onGo={(pr) => {
                      setOpen(false);
                      onGo(pr);
                    }}
                  />
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </div>
  );
}
