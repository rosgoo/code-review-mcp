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
  | { type: "thread_deleted"; comment_id?: string; thread_id?: string }
  | { type: "comment_resolved"; comment_id: string }
  | {
      type: "reply_added";
      comment_id: string;
      reply: Reply;
      reopened: boolean;
    }
  | { type: "head_moved"; old_head_sha: string | null; new_head_sha: string }
  | { type: "review_closed" }
  | { type: "thread_added"; thread?: ReviewThread }
  | { type: "thread_updated"; thread?: ReviewThread }
  | { type: "threads_stale"; thread_ids: string[] }
  | { type: "review_submitted"; html_url?: string; event?: ReviewEventName; posted?: number }
  | ({ type: "agent_status" } & AgentStatus)
  | { type: "agent_delta"; thread_id: string; text: string }
  | { type: "agent_message"; thread_id: string; message: ThreadMessage }
  | { type: "agent_error"; thread_id: string; error: string };

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
  /** The GitHub stack the PR is in; null or absent when it is in none. */
  stack?: InboxStack | null;
}

export interface InboxStack {
  number: number;
  size: number;
  position: number;
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

export type ReviewEventName = "COMMENT" | "APPROVE" | "REQUEST_CHANGES";

export interface Viewer {
  login: string;
  is_author: boolean;
}

export interface ThreadMessage {
  id: string;
  author: "user" | "agent";
  body: string;
  created_at: string;
}

export interface ReviewThread {
  id: string;
  kind: "local" | "question" | "review_comment";
  status: ThreadStatus;
  path: string;
  side: Side;
  /** 0 for a comment on the whole file. */
  line: number;
  start_line: number | null;
  start_side: Side | null;
  anchor_sha: string | null;
  created_by: "user" | "agent";
  created_at: string;
  updated_at: string;
  github_url: string | null;
  messages: ThreadMessage[];
}

export interface ReviewAnchor {
  path: string;
  side: Side;
  /** 0 for a comment on the whole file. */
  line: number;
  start_line?: number;
  start_side?: Side;
}

export interface NewReviewThread extends ReviewAnchor {
  kind: "review_comment" | "question";
  body: string;
}

export type ComposerMode = "comment" | "question";

export type AgentState = "idle" | "queued" | "running" | "error" | "off";
export type WarmupStatus = "none" | "running" | "done" | "error";

export interface AgentStatus {
  state: AgentState;
  session_id: string | null;
  model: string | null;
  cost_usd: number | null;
  context_tokens: number | null;
  /** Thread ids waiting for a turn, oldest first. */
  queue: string[];
  running_thread_id: string | null;
  warmup: { status: WarmupStatus; thread_id: string | null };
}

export interface SubmitReviewResult {
  github_review_id: number;
  html_url: string;
  posted: number;
}

export type LineRange = [number, number];

export interface Commentable {
  additions: LineRange[];
  deletions: LineRange[];
}

export interface PrView {
  review_id: string;
  kind: "pr";
  url: string;
  viewer?: Viewer | null;
  allowed_events?: ReviewEventName[];
  draft_count?: number;
  stale_count?: number;
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
  /** 1-based inclusive line ranges inside the diff's hunks, per side. */
  commentable?: Commentable;
}

export interface RefreshResult {
  head_moved: boolean;
  old_head_sha: string | null;
  new_head_sha: string;
  pr: PrView;
}

/** "github" is a native GitHub stack. "branches" is a chain of PRs, each based on the previous PR's branch. */
export type StackSource = "github" | "branches";

export interface StackPr {
  number: number;
  title: string;
  /** Lower case: open, closed, or merged. */
  state: string;
  is_draft: boolean;
  head_ref: string;
  base_ref: string;
  url: string;
  ci_state: CheckState | null;
  review_id: string | null;
}

export interface PrStackEntry extends StackPr {
  position: number;
}

/** An open PR based on a stack entry's branch that is not in the stack. */
export interface PrStackExtension extends StackPr {
  based_on: number;
}

export interface PrStack {
  source: StackSource;
  /** The GitHub stack number; null for a branch stack. */
  number: number | null;
  size: number;
  base_ref: string;
  /** The reviewed PR's place in the stack; null when it is an extension. */
  position: number | null;
  entries: PrStackEntry[];
  extensions: PrStackExtension[];
}
