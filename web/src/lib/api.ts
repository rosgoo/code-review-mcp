import type {
  Comment,
  InboxList,
  InboxName,
  NewComment,
  OpenedPr,
  PrFileContent,
  PrView,
  RefreshResult,
  ReviewSummary,
  ReviewView,
} from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
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
    throw new ApiError(response.status, error || response.statusText);
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
  prFile: (reviewId: string, path: string) =>
    request<PrFileContent>(`${reviewPath(reviewId)}/file?path=${encodeURIComponent(path)}`),
  refreshPr: (reviewId: string) => post<RefreshResult>(`${reviewPath(reviewId)}/refresh`),
  setViewed: (reviewId: string, path: string, viewed: boolean) =>
    send<{ path: string; viewed: boolean }>(viewed ? "PUT" : "DELETE", `${reviewPath(reviewId)}/viewed`, {
      path,
    }),
  closePr: (reviewId: string) => post<{ ok: boolean }>(`${reviewPath(reviewId)}/close`),
};

export const eventsUrl = (reviewId: string) =>
  `/api/events?review=${encodeURIComponent(reviewId)}`;
