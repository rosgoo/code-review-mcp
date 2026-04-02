// Entry point — init + footer submit logic

import { renderDiff, loadExistingComments, renderInlineThreads, buildFileTree } from './diff.js';
import { connectSSE } from './sse.js';
import { renderCommentSidebar, updatePendingCount } from './comments.js';
import { submitAllDrafts, postComment } from './api.js';
import { comments, addComment, getNextLocalId, setViewMode } from './state.js';
import { initResizeHandles } from './resize.js';

const DEFAULT_BTN_TEXT = "Submit Comments";
let morphTimer = null;

export function morphButton(btn, state, text) {
  if (morphTimer) { clearTimeout(morphTimer); morphTimer = null; }
  btn.className = "btn-submit" + (state ? " " + state : "");
  btn.textContent = text || DEFAULT_BTN_TEXT;
  btn.disabled = state === "sending";
}

function resetButton(btn, delay = 2500) {
  morphTimer = setTimeout(() => {
    morphButton(btn, "", DEFAULT_BTN_TEXT);
  }, delay);
}

function initFooter() {
  const btn = document.getElementById("submit-btn");

  async function submitAllFeedback() {
    const overallText = document.getElementById("overall-feedback").value.trim();
    const drafts = comments.filter(c => c.status === "draft");

    if (overallText) {
      try {
        const data = await postComment({
          file_path: "(overall)",
          line_number: 0,
          line_type: "context",
          line_content: "",
          user_message: overallText,
        });
        addComment({
          localId: "local-" + getNextLocalId(),
          serverId: data.id,
          filePath: "(overall)",
          lineNumber: 0,
          lineType: "context",
          lineContent: "",
          userMessage: overallText,
          timestamp: new Date().toISOString(),
          status: "draft",
          replies: [],
        });
      } catch { /* proceed anyway */ }
    }

    if (drafts.length === 0 && !overallText) {
      const submitted = comments.filter(c => c.status === "submitted");
      if (submitted.length > 0) {
        morphButton(btn, "awaiting", "Awaiting revision");
      } else {
        morphButton(btn, "empty", "Nothing to send");
        resetButton(btn, 2000);
      }
      return;
    }

    morphButton(btn, "sending", "Sending...");

    try {
      await submitAllDrafts();
      comments.forEach(c => {
        if (c.status === "draft") c.status = "submitted";
      });
      document.getElementById("overall-feedback").value = "";
      renderInlineThreads();
      renderCommentSidebar();
      morphButton(btn, "sent", "Sent");
      resetButton(btn);
    } catch {
      morphButton(btn, "error", "Retry");
      resetButton(btn, 3000);
    }
  }

  btn.addEventListener("click", submitAllFeedback);

  document.getElementById("overall-feedback").addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      submitAllFeedback();
    }
  });
}

function initViewToggle() {
  const buttons = document.querySelectorAll(".view-btn");
  buttons.forEach(btn => {
    btn.addEventListener("click", () => {
      buttons.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      setViewMode(btn.dataset.view);
      renderDiff().then(() => {
        buildFileTree();
        renderInlineThreads();
        renderCommentSidebar();
      });
    });
  });
}

function initThemeToggle() {
  const toggle = document.getElementById("theme-toggle");
  // Load saved preference
  const saved = localStorage.getItem("code-review-theme");
  if (saved) document.documentElement.setAttribute("data-theme", saved);

  toggle.addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("code-review-theme", next);
  });
}

function initSidebarToggle() {
  const toggle = document.getElementById("sidebar-toggle");
  const sidebar = document.getElementById("file-sidebar");
  const resizeHandle = document.getElementById("resize-sidebar");

  toggle.addEventListener("click", () => {
    const willCollapse = !sidebar.classList.contains("collapsed");
    // Clear any inline width from resize dragging so CSS class takes effect
    if (willCollapse) {
      sidebar.dataset.prevWidth = sidebar.style.width || "";
      sidebar.style.width = "";
    } else {
      // Restore previous width when expanding
      if (sidebar.dataset.prevWidth) {
        sidebar.style.width = sidebar.dataset.prevWidth;
      }
    }
    sidebar.classList.toggle("collapsed");
    // Hide/show the resize handle too
    if (resizeHandle) {
      resizeHandle.style.display = willCollapse ? "none" : "";
    }
  });
}

async function init() {
  initThemeToggle();
  initSidebarToggle();

  await renderDiff();
  buildFileTree();
  await loadExistingComments();
  renderInlineThreads();
  renderCommentSidebar();
  connectSSE();
  initFooter();
  initViewToggle();
  initResizeHandles();

  window.addEventListener("resize", () => { renderCommentSidebar(); });
}

init();
