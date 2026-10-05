import { createContext, useContext } from "react";
import type { CommentAnchor } from "../lib/anchors";
import type { Comment } from "../lib/types";

export interface ReviewActions {
  commentsById: ReadonlyMap<string, Comment>;
  composer: CommentAnchor | null;
  openComposer(anchor: CommentAnchor | null): void;
  saveComment(anchor: CommentAnchor, body: string): Promise<void>;
  reply(threadId: string, message: string): Promise<void>;
  deleteThread(threadId: string): Promise<void>;
}

export const ReviewContext = createContext<ReviewActions | null>(null);

export function useReview(): ReviewActions {
  const actions = useContext(ReviewContext);
  if (actions === null) throw new Error("useReview needs a ReviewContext provider");
  return actions;
}
