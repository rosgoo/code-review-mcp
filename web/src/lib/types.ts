import type { AnnotationSide } from "@pierre/diffs";

export type Side = AnnotationSide;
export type ThreadStatus =
  | "draft"
  | "submitted"
  | "resolved"
  | "stale"
  | "posted";
export type LineType = "add" | "delete" | "context";
export type ReviewMode = "diff" | "files" | "empty";

export interface ReviewSummary {
  id: string;
  kind: "pr" | "local";
  title: string;
  status: string;
  mode: ReviewMode | null;
  url: string;
  created_at: string;
  updated_at: string;
}

export interface Reply {
  id: string;
  comment_id: string;
  author: "user" | "claude";
  message: string;
  timestamp: string;
}

export interface Comment {
  id: string;
  file_path: string;
  line_number: number;
  line_type: LineType;
  line_content: string;
  user_message: string;
  timestamp: string;
  status: ThreadStatus;
  replies: Reply[];
  start_line?: number;
  start_side?: Side;
}

export interface ViewFile {
  path: string;
  content: string;
  language: string;
  added_lines?: number[];
  deleted_lines?: number[];
  old_content?: string | null;
}

interface ViewBase {
  review_id: string;
  title: string;
}

export type ReviewView =
  | (ViewBase & { mode: "diff"; diff: string })
  | (ViewBase & { mode: "files"; files: ViewFile[]; diff?: string })
  | (ViewBase & { mode: "empty" });

export interface NewComment {
  path: string;
  side: Side;
  line: number;
  start_line?: number;
  start_side?: Side;
  line_content: string;
  body: string;
}

export type ReviewEvent =
  | { type: "connected" }
  | { type: "view_updated" }
  | { type: "comments_submitted"; count: number }
  | { type: "comment_added"; comment: Comment }
  | { type: "thread_deleted"; comment_id: string }
  | { type: "comment_resolved"; comment_id: string }
  | {
      type: "reply_added";
      comment_id: string;
      reply: Reply;
      reopened: boolean;
    };
