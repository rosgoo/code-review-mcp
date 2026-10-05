import { createContext, useContext } from "react";
import type { AnchorResult } from "../lib/review";
import type { ReviewAnchor, ReviewThread } from "../lib/types";

export interface PrCommentActions {
  openComposer(anchor: ReviewAnchor | null): void;
  addComment(anchor: ReviewAnchor, body: string): Promise<void>;
  editComment(threadId: string, body: string): Promise<void>;
  deleteComment(threadId: string): Promise<void>;
  startReanchor(threadId: string | null): void;
  /** A line or range the user picked in `path`: opens the composer or re-anchors. */
  pickLines(path: string, result: AnchorResult): void;
}

export interface PrCommentState {
  threadsById: ReadonlyMap<string, ReviewThread>;
  composer: ReviewAnchor | null;
  reanchoring: string | null;
}

export const PrCommentActionsContext = createContext<PrCommentActions | null>(null);
export const PrCommentStateContext = createContext<PrCommentState | null>(null);

export function usePrCommentActions(): PrCommentActions {
  const actions = useContext(PrCommentActionsContext);
  if (actions === null) throw new Error("usePrCommentActions needs a provider");
  return actions;
}

export function usePrCommentState(): PrCommentState {
  const state = useContext(PrCommentStateContext);
  if (state === null) throw new Error("usePrCommentState needs a provider");
  return state;
}
