// All HTTP fetch calls

import { reviewId } from './state.js';

function reviewPath(suffix) {
  return `/api/reviews/${encodeURIComponent(reviewId)}${suffix}`;
}

export async function resolveReviewId() {
  const fromUrl = new URLSearchParams(location.search).get("review");
  if (fromUrl) return fromUrl;
  const res = await fetch("/api/reviews");
  const reviews = await res.json();
  return reviews.length > 0 ? reviews[0].id : null;
}

export async function fetchView() {
  if (!reviewId) return { mode: "empty", title: "Code Review" };
  const res = await fetch(reviewPath("/view"));
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    return { mode: "empty", title: "Code Review", error: body.error || "Review not found" };
  }
  return res.json();
}

export async function fetchDiff() {
  const data = await fetchView();
  return { diff: data.diff || "", title: data.title };
}

export async function fetchAllComments() {
  if (!reviewId) return [];
  const res = await fetch(reviewPath("/comments"));
  return res.json();
}

export async function postComment(item) {
  const res = await fetch(reviewPath("/comments"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(item),
  });
  return res.json();
}

export async function submitAllDrafts() {
  const res = await fetch(reviewPath("/submit"), { method: "POST" });
  return res.json();
}

export async function postReply(commentId, body) {
  const res = await fetch(`/api/threads/${encodeURIComponent(commentId)}/reply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}
