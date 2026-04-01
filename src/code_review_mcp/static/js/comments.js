// Comment sidebar — shows summary of all comments for navigation

import { comments, removeComment } from './state.js';
import { renderInlineThreads } from './diff.js';

export function renderCommentSidebar() {
  const list = document.getElementById("comment-list");
  list.textContent = "";

  if (comments.length === 0) {
    const empty = document.createElement("div");
    empty.className = "sidebar-empty";
    empty.textContent = "Click + on any line to add a comment";
    list.appendChild(empty);
    return;
  }

  // Sort by file then line number
  const sorted = [...comments].sort((a, b) => {
    if (a.filePath !== b.filePath) return a.filePath.localeCompare(b.filePath);
    return a.lineNumber - b.lineNumber;
  });

  let currentFile = null;
  sorted.forEach(c => {
    // File header
    if (c.filePath !== currentFile) {
      currentFile = c.filePath;
      const fileHeader = document.createElement("div");
      fileHeader.className = "sidebar-file-header";
      fileHeader.textContent = currentFile;
      list.appendChild(fileHeader);
    }

    const card = document.createElement("div");
    card.className = `sidebar-comment ${c.status}`;
    card.dataset.localId = c.localId;

    // Header row
    const header = document.createElement("div");
    header.className = "sidebar-comment-header";

    const lineRef = document.createElement("span");
    lineRef.className = "sidebar-line-ref";
    const typeIcon = c.lineType === "add" ? "+" : c.lineType === "delete" ? "-" : " ";
    lineRef.textContent = `${typeIcon} L${c.lineNumber}`;
    header.appendChild(lineRef);

    const badge = document.createElement("span");
    badge.className = `status-badge-sm ${c.status}`;
    badge.textContent = c.status;
    header.appendChild(badge);

    if (c.replies && c.replies.length > 0) {
      const replyCount = document.createElement("span");
      replyCount.className = "reply-count";
      replyCount.textContent = `${c.replies.length} repl${c.replies.length === 1 ? "y" : "ies"}`;
      header.appendChild(replyCount);
    }

    const discard = document.createElement("span");
    discard.className = "sidebar-discard";
    discard.textContent = "\u00d7";
    discard.title = "Remove comment";
    discard.addEventListener("click", (e) => {
      e.stopPropagation();
      removeComment(c.localId);
      renderInlineThreads();
      renderCommentSidebar();
    });
    header.appendChild(discard);

    card.appendChild(header);

    // Comment preview
    const preview = document.createElement("div");
    preview.className = "sidebar-comment-preview";
    preview.textContent = c.userMessage.length > 80
      ? c.userMessage.slice(0, 80) + "..."
      : c.userMessage;
    card.appendChild(preview);

    // Click to scroll to inline thread
    card.addEventListener("click", () => {
      const thread = document.querySelector(`.inline-thread[data-local-id="${CSS.escape(c.localId)}"]`);
      if (thread) {
        thread.scrollIntoView({ behavior: "smooth", block: "center" });
        thread.classList.add("pulse");
        setTimeout(() => thread.classList.remove("pulse"), 1500);
      }
    });

    list.appendChild(card);
  });

  updatePendingCount();
}

export function updatePendingCount() {
  const badge = document.getElementById("pending-count");
  const n = comments.filter(c => c.status === "draft").length;
  if (n === 0) {
    badge.classList.remove("visible");
  } else {
    badge.textContent = `${n} draft${n === 1 ? "" : "s"}`;
    badge.classList.add("visible");
  }
}
