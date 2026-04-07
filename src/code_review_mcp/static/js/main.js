// Entry point — init + footer submit + mode routing

import { renderDiff, loadExistingComments, renderInlineThreads, buildFileTree } from './diff.js';
import { renderFiles, loadExistingFileComments, renderFileInlineThreads, buildFileSidebar } from './files.js';
import { connectSSE } from './sse.js';
import { renderCommentSidebar, updatePendingCount } from './comments.js';
import { submitAllDrafts, postComment, fetchView } from './api.js';
import { comments, addComment, getNextLocalId, setViewMode, currentMode, setMode } from './state.js';
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

// ── Mode-aware render ──────────────────────────────────────────────────────

export async function renderCurrentView() {
  // Fetch mode from server
  const data = await fetchView();
  setMode(data.mode);

  if (data.mode === "diff") {
    document.querySelector(".view-toggle").style.display = "";
    await renderDiff();
    buildFileTree();
    await loadExistingComments();
    renderInlineThreads();
  } else if (data.mode === "files") {
    await renderFiles();
    buildFileSidebar();
    await loadExistingFileComments();
    renderFileInlineThreads();
  } else {
    document.getElementById("diff-content").textContent = "";
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "Waiting for Claude to send code...";
    document.getElementById("diff-content").appendChild(empty);
  }

  renderCommentSidebar();
}

// ── Footer ─────────────────────────────────────────────────────────────────

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
      // Re-render threads for current mode
      if (currentMode === "diff") {
        renderInlineThreads();
      } else if (currentMode === "files") {
        renderFileInlineThreads();
      }
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
      renderCurrentView();
    });
  });
}

function initThemeToggle() {
  const toggle = document.getElementById("theme-toggle");
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
    if (willCollapse) {
      sidebar.dataset.prevWidth = sidebar.style.width || "";
      sidebar.style.width = "";
    } else {
      if (sidebar.dataset.prevWidth) {
        sidebar.style.width = sidebar.dataset.prevWidth;
      }
    }
    sidebar.classList.toggle("collapsed");
    if (resizeHandle) {
      resizeHandle.style.display = willCollapse ? "none" : "";
    }
  });
}

async function init() {
  initThemeToggle();
  initSidebarToggle();

  await renderCurrentView();
  connectSSE();
  initFooter();
  initViewToggle();
  initResizeHandles();

  window.addEventListener("resize", () => { renderCommentSidebar(); });
}

init();
