import { useCallback, useRef, useState } from "react";
import { api } from "../lib/api";
import { useElapsedSeconds } from "../lib/hooks";
import { navigate, reviewPath } from "../lib/router";
import { errorMessage } from "./Thread";

export interface Opening {
  ref: string;
  startedAt: number;
}

/** Open a PR review through the daemon, then navigate to it. One open runs at a time. */
export function usePrOpener() {
  const [opening, setOpening] = useState<Opening | null>(null);
  const [error, setError] = useState<string | null>(null);
  const busy = useRef(false);

  const open = useCallback(async (ref: string) => {
    const target = ref.trim();
    if (!target || busy.current) return;
    busy.current = true;
    setError(null);
    setOpening({ ref: target, startedAt: Date.now() });
    try {
      const opened = await api.openPr(target);
      navigate(reviewPath(opened.review_id), { state: opened.note ? { note: opened.note } : null });
    } catch (e) {
      setError(errorMessage(e));
      setOpening(null);
      busy.current = false;
    }
  }, []);

  return { opening, error, open };
}

export function OpenProgress({ opening }: { opening: Opening }) {
  const elapsed = useElapsedSeconds(opening.startedAt);
  return (
    <p className="open-progress" role="status">
      <span className="spinner" aria-hidden="true" /> Opening <code>{opening.ref}</code>… {elapsed}{" "}
      s
      <span className="muted">
        {" "}
        The first open fetches the PR and checks out a worktree. It can take 10 s or more.
      </span>
    </p>
  );
}
