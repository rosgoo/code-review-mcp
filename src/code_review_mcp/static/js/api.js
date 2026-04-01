// All HTTP fetch calls

export async function fetchDiff() {
  const res = await fetch("/diff");
  return res.json();
}

export async function fetchAllComments() {
  const res = await fetch("/comments/all");
  return res.json();
}

export async function postComment(item) {
  const res = await fetch("/comments", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(item),
  });
  return res.json();
}

export async function submitAllDrafts() {
  const res = await fetch("/comments/submit-all", { method: "POST" });
  return res.json();
}

export async function postReply(commentId, body) {
  const res = await fetch(`/comments/${commentId}/reply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return res.json();
}
