import type {
  AgentStatus,
  Comment,
  InboxList,
  InboxName,
  NewComment,
  NewReviewThread,
  OpenedPr,
  PrFileContent,
  PrStack,
  PrView,
  RefreshResult,
  ReviewAnchor,
  ReviewEventName,
  ReviewSummary,
  ReviewThread,
  SubmitReviewResult,
  ReviewView,
  SentBatch,
} from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly body: unknown = null,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const error =
      body !== null && typeof body === "object" && "error" in body
        ? String(body.error)
        : "";
    throw new ApiError(response.status, error || response.statusText, body);
  }
  return body as T;
}

function send<T>(method: string, path: string, payload?: unknown): Promise<T> {
  return request<T>(path, {
    method,
    headers:
      payload === undefined
        ? undefined
        : { "Content-Type": "application/json" },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
}

const post = <T>(path: string, payload?: unknown) => send<T>("POST", path, payload);

const reviewPath = (id: string) => `/api/reviews/${encodeURIComponent(id)}`;
const threadPath = (id: string) => `/api/threads/${encodeURIComponent(id)}`;

export const api = {
  listReviews: () => request<ReviewSummary[]>("/api/reviews"),
  view: (reviewId: string) =>
    request<ReviewView>(`${reviewPath(reviewId)}/view`),
  comments: (reviewId: string) =>
    request<Comment[]>(`${reviewPath(reviewId)}/comments`),
  addComment: (reviewId: string, comment: NewComment) =>
    post<{ id: string }>(`${reviewPath(reviewId)}/comments`, comment),
  submit: (reviewId: string) =>
    post<{ submitted: number }>(`${reviewPath(reviewId)}/submit`),
  reply: (threadId: string, message: string) =>
    post<{ id: string; reopened: boolean }>(`${threadPath(threadId)}/reply`, {
      message,
    }),
  deleteThread: (threadId: string) =>
    request<{ deleted: boolean }>(threadPath(threadId), { method: "DELETE" }),
  inboxList: (name: InboxName, refresh = false) =>
    request<InboxList>(`/api/inbox/${name}${refresh ? "?refresh=true" : ""}`),
  openPr: (ref: string) => post<OpenedPr>("/api/prs/open", { ref }),
  prView: (reviewId: string) => request<PrView>(`${reviewPath(reviewId)}/pr`),
  reviewStack: (reviewId: string) => request<PrStack | null>(`${reviewPath(reviewId)}/stack`),
  prFile: (reviewId: string, path: string) =>
    request<PrFileContent>(`${reviewPath(reviewId)}/file?path=${encodeURIComponent(path)}`),
  refreshPr: (reviewId: string) => post<RefreshResult>(`${reviewPath(reviewId)}/refresh`),
  setViewed: (reviewId: string, path: string, viewed: boolean) =>
    send<{ path: string; viewed: boolean }>(viewed ? "PUT" : "DELETE", `${reviewPath(reviewId)}/viewed`, {
      path,
    }),
  reviewThreads: (reviewId: string) => request<ReviewThread[]>(`${reviewPath(reviewId)}/threads`),
  createReviewThread: (reviewId: string, thread: NewReviewThread) =>
    post<ReviewThread>(`${reviewPath(reviewId)}/threads`, thread),
  editReviewThread: (threadId: string, body: string) =>
    send<ReviewThread>("PATCH", threadPath(threadId), { body }),
  reanchorReviewThread: (threadId: string, anchor: Omit<ReviewAnchor, "path">) =>
    send<ReviewThread>("PATCH", threadPath(threadId), anchor),
  deleteReviewThread: (threadId: string) =>
    request<unknown>(threadPath(threadId), { method: "DELETE" }),
  agentStatus: (reviewId: string) => request<AgentStatus>(`${reviewPath(reviewId)}/agent`),
  startWarmup: (reviewId: string) => post<AgentStatus>(`${reviewPath(reviewId)}/agent/warmup`),
  sendToAgent: (reviewId: string, threadIds?: readonly string[]) =>
    post<SentBatch>(
      `${reviewPath(reviewId)}/agent/send`,
      threadIds === undefined ? {} : { thread_ids: threadIds },
    ),
  stopAgent: (reviewId: string) => post<unknown>(`${reviewPath(reviewId)}/agent/stop`),
  editMessage: (messageId: string, body: string) =>
    send<unknown>("PATCH", `/api/messages/${encodeURIComponent(messageId)}`, { body }),
  deleteMessage: (messageId: string) =>
    request<unknown>(`/api/messages/${encodeURIComponent(messageId)}`, { method: "DELETE" }),
  submitReview: (reviewId: string, event: ReviewEventName, body: string) =>
    post<SubmitReviewResult>(`${reviewPath(reviewId)}/submit-review`, { event, body }),
  closePr: (reviewId: string) => post<{ ok: boolean }>(`${reviewPath(reviewId)}/close`),
};

export const eventsUrl = (reviewId: string) =>
  `/api/events?review=${encodeURIComponent(reviewId)}`;
