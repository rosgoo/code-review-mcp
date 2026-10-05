import type { Comment, ReviewEvent } from "./types";

export const OVERALL_PATH = "(overall)";

/** Apply a thread event to the list. Returns the same array when the event changes nothing. */
export function applyCommentEvent(
  comments: Comment[],
  event: ReviewEvent,
): Comment[] {
  switch (event.type) {
    case "comment_added":
      return comments.some((c) => c.id === event.comment.id)
        ? comments
        : [...comments, event.comment];
    case "thread_deleted":
      return comments.some((c) => c.id === event.comment_id)
        ? comments.filter((c) => c.id !== event.comment_id)
        : comments;
    case "comment_resolved":
      return comments.map((c) =>
        c.id === event.comment_id ? { ...c, status: "resolved" as const } : c,
      );
    case "reply_added":
      return comments.map((c) => {
        if (c.id !== event.comment_id) return c;
        const replies = c.replies.some((r) => r.id === event.reply.id)
          ? c.replies
          : [...c.replies, event.reply];
        return {
          ...c,
          replies,
          status: event.reopened ? "submitted" : c.status,
        };
      });
    default:
      return comments;
  }
}

export function countByStatus(comments: readonly Comment[]) {
  return {
    drafts: comments.filter((c) => c.status === "draft").length,
    awaiting: comments.filter((c) => c.status === "submitted").length,
  };
}

export function lineLabel(comment: Comment): string {
  if (comment.file_path === OVERALL_PATH) return "Overall";
  if (
    comment.start_line !== undefined &&
    comment.start_line !== comment.line_number
  ) {
    return `L${comment.start_line}–L${comment.line_number}`;
  }
  return `L${comment.line_number}`;
}
