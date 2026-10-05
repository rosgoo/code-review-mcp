import type { AnnotationSide } from "@pierre/diffs";

export type Side = AnnotationSide;
export type ThreadStatus =
  | "draft"
  | "submitted"
  | "resolved"
  | "stale"
  | "posted";
export type LineType = "add" | "delete" | "context";
export type DiffStyle = "unified" | "split";
export type ReviewMode = "diff" | "files" | "empty";

export type ReviewKind = "pr" | "local";

export interface ReviewSummary {
  id: string;
  kind: ReviewKind;
  title: string;
  repo: string | null;
  pr_number: number | null;
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
  kind: ReviewKind;
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
    }
  | { type: "head_moved"; old_head_sha: string | null; new_head_sha: string }
  | { type: "review_closed" };

export interface InboxItem {
  repo: string;
  number: number;
  title: string;
  url: string;
  author: string | null;
  author_is_bot: boolean;
  is_draft: boolean;
  created_at: string;
  updated_at: string;
  base_ref: string;
  head_ref: string;
  additions: number;
  deletions: number;
  changed_files: number;
  review_decision: string | null;
  viewer_review: string | null;
  labels: string[];
  ci_state: CheckState | null;
  review_id: string | null;
}

export type InboxName = "direct" | "mine" | "team";

export interface InboxList {
  name: InboxName;
  total: number;
  fetched_at: string;
  /** True while the daemon refreshes a stale copy in the background. */
  refreshing: boolean;
  items: InboxItem[];
}

export interface OpenedPr {
  review_id: string;
  url: string;
  note?: string;
}

export type FileStatus =
  | "added"
  | "modified"
  | "deleted"
  | "renamed"
  | "copied"
  | "type_changed"
  | "unmerged"
  | "unknown";

export interface PrFile {
  path: string;
  old_path: string | null;
  status: FileStatus;
  additions: number | null;
  deletions: number | null;
  binary: boolean;
  viewed: boolean;
}

export type CheckState = "success" | "failure" | "pending" | "skipped";

export interface PrCheck {
  name: string;
  state: CheckState;
  url: string | null;
  workflow: string | null;
}

export interface PrGithub {
  fetched_at: string;
  review_decision: string | null;
  review_requests: string[];
  checks_state: CheckState | null;
  checks: PrCheck[];
  additions: number;
  deletions: number;
}

export interface PrView {
  review_id: string;
  kind: "pr";
  url: string;
  status: "open" | "submitted" | "closed";
  repo: string;
  number: number;
  title: string;
  author: string | null;
  github_url: string | null;
  body: string | null;
  state: string | null;
  is_draft: boolean;
  base_ref: string | null;
  head_ref: string | null;
  base_sha: string | null;
  head_sha: string | null;
  merge_base_sha: string | null;
  worktree_path: string | null;
  github: PrGithub | null;
  files: PrFile[];
}

export interface PrFileContent {
  path: string;
  old_path: string | null;
  status: FileStatus;
  language: string;
  merge_base_sha: string;
  head_sha: string;
  binary: boolean;
  too_large: boolean;
  old_content: string | null;
  new_content: string | null;
}

export interface RefreshResult {
  head_moved: boolean;
  old_head_sha: string | null;
  new_head_sha: string;
  pr: PrView;
}
