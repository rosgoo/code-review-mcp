import type { SelectedLineRange } from "@pierre/diffs";
import { createContext, useContext } from "react";
import { EMPTY_LIVE, type AgentLive, type Composer } from "../lib/agent";
import type { Citation } from "../lib/citations";
import type { Commentable, ComposerMode, ReviewAnchor, ReviewThread } from "../lib/types";
import type { CitationHandler } from "./Markdown";

export interface PrCommentActions {
  openComposer(composer: Composer | null): void;
  /** Switch the open composer, and the mode the next one starts in. */
  setComposerMode(mode: ComposerMode): void;
  addComment(anchor: ReviewAnchor, body: string): Promise<void>;
  askQuestion(anchor: ReviewAnchor, body: string): Promise<void>;
  /** A follow-up on a question thread: the agent answers it in a new turn. */
  reply(threadId: string, body: string): Promise<void>;
  stop(threadId: string): Promise<void>;
  /** Send the thread's last question again. */
  retry(thread: ReviewThread): Promise<void>;
  editComment(threadId: string, body: string): Promise<void>;
  deleteComment(threadId: string): Promise<void>;
  startReanchor(threadId: string | null): void;
  /** Lines the user picked in `path`: opens the composer, or moves the comment being re-anchored. */
  pickLines(path: string, range: SelectedLineRange, commentable: Commentable | undefined): void;
  openCitation(citation: Citation): void;
}

export interface PrCommentState {
  threadsById: ReadonlyMap<string, ReviewThread>;
  composer: Composer | null;
  reanchoring: string | null;
  /** False when the agent is off or the daemon has no agent: questions cannot be asked. */
  agentOn: boolean;
  citations: CitationHandler;
}

export const PrCommentActionsContext = createContext<PrCommentActions | null>(null);
export const PrCommentStateContext = createContext<PrCommentState | null>(null);
export const AgentLiveContext = createContext<AgentLive>(EMPTY_LIVE);

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

export const useAgentLive = () => useContext(AgentLiveContext);
