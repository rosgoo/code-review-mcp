// SSE connection and event handling

import { comments, findCommentByServerId, currentMode } from './state.js';
import { renderInlineThreads } from './diff.js';
import { renderFileInlineThreads } from './files.js';
import { renderCommentSidebar } from './comments.js';
import { morphButton, renderCurrentView } from './main.js';

export function connectSSE() {
  const es = new EventSource("/events");
  es.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "view_updated" || msg.type === "diff_updated") {
      renderCurrentView();
    } else if (msg.type === "reply_added") {
      handleReplyAdded(msg);
    } else if (msg.type === "comment_resolved") {
      handleCommentResolved(msg);
    }
  };
  es.onerror = () => { es.close(); setTimeout(connectSSE, 2000); };
}

function handleCommentResolved(msg) {
  const c = findCommentByServerId(msg.comment_id);
  if (!c) return;
  c.status = "resolved";
  rerenderThreads();
  renderCommentSidebar();
}

function handleReplyAdded(msg) {
  const c = findCommentByServerId(msg.comment_id);
  if (!c) return;
  if (!c.replies) c.replies = [];
  if (!c.replies.find(r => r.id === msg.reply.id)) {
    c.replies.push(msg.reply);
  }
  if (msg.reopened) {
    c.status = "submitted";
    const btn = document.getElementById("submit-btn");
    if (btn) morphButton(btn, "awaiting", "Awaiting revision");
  }
  rerenderThreads();
  renderCommentSidebar();
}

function rerenderThreads() {
  if (currentMode === "diff") {
    renderInlineThreads();
  } else if (currentMode === "files") {
    renderFileInlineThreads();
  }
}
