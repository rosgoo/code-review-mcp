// File viewer — renders files with syntax highlighting and inline commenting
// Note: Claude replies use DOMPurify.sanitize(marked.parse(...)) for safe HTML rendering

import { filesData, setFilesData, setMode, comments, addComment, getNextLocalId } from './state.js';
import { fetchView, fetchAllComments, postComment } from './api.js';
import { renderCommentSidebar } from './comments.js';

export async function renderFiles() {
  const data = await fetchView();
  setMode(data.mode);
  setFilesData({ files: data.files || [], title: data.title });

  document.getElementById("review-title").textContent = data.title || "File Viewer";
  document.getElementById("page-title").textContent = data.title || "File Viewer";

  // Hide diff-only controls
  document.querySelector(".view-toggle").style.display = "none";
  document.getElementById("file-stats").textContent = "";

  const container = document.getElementById("diff-content");
  container.textContent = "";

  if (!data.files || data.files.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No files loaded";
    container.appendChild(empty);
    return;
  }

  data.files.forEach(file => {
    const wrapper = document.createElement("div");
    wrapper.className = "file-view-wrapper";
    wrapper.dataset.filePath = file.path;

    // File header
    const header = document.createElement("div");
    header.className = "file-view-header";

    const collapseBtn = document.createElement("button");
    collapseBtn.className = "file-collapse-btn";
    collapseBtn.textContent = "\u25BE";
    collapseBtn.addEventListener("click", () => {
      const collapsed = wrapper.classList.toggle("file-collapsed");
      collapseBtn.textContent = collapsed ? "\u25B8" : "\u25BE";
    });
    header.appendChild(collapseBtn);

    const fileName = document.createElement("span");
    fileName.className = "file-view-name";
    fileName.textContent = file.path;
    header.appendChild(fileName);

    const lineCount = document.createElement("span");
    lineCount.className = "file-view-line-count";
    const lines = file.content.split("\n");
    lineCount.textContent = `${lines.length} lines`;
    header.appendChild(lineCount);

    wrapper.appendChild(header);

    // File body — code table with line numbers
    const body = document.createElement("div");
    body.className = "file-view-body";

    const table = document.createElement("table");
    table.className = "file-view-table";

    const tbody = document.createElement("tbody");

    lines.forEach((line, i) => {
      const tr = document.createElement("tr");
      tr.className = "file-view-line";
      tr.dataset.lineNum = i + 1;

      // Line number cell
      const numTd = document.createElement("td");
      numTd.className = "file-view-num";
      numTd.textContent = i + 1;
      numTd.style.position = "relative";

      // Add comment button
      const addBtn = document.createElement("button");
      addBtn.className = "line-add-btn";
      addBtn.textContent = "+";
      addBtn.title = "Add comment";
      addBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        openFileCommentForm(tr, file.path, i + 1, line);
      });
      numTd.appendChild(addBtn);

      tr.appendChild(numTd);

      // Code cell
      const codeTd = document.createElement("td");
      codeTd.className = "file-view-code";

      const codePre = document.createElement("pre");
      const codeEl = document.createElement("code");
      codeEl.className = file.language ? `language-${file.language}` : "";
      codeEl.textContent = line || " "; // empty lines need a space for height
      codePre.appendChild(codeEl);
      codeTd.appendChild(codePre);

      tr.appendChild(codeTd);
      tbody.appendChild(tr);
    });

    table.appendChild(tbody);
    body.appendChild(table);
    wrapper.appendChild(body);
    container.appendChild(wrapper);

    // Run highlight.js on the code blocks
    if (window.hljs) {
      wrapper.querySelectorAll("code[class^='language-']").forEach(block => {
        hljs.highlightElement(block);
      });
    }
  });
}

function openFileCommentForm(row, filePath, lineNumber, lineContent) {
  if (row.nextElementSibling?.classList.contains("inline-comment-form-row")) return;

  const formRow = document.createElement("tr");
  formRow.className = "inline-comment-form-row";

  const formCell = document.createElement("td");
  formCell.colSpan = 2;
  formCell.className = "inline-comment-form-cell";

  const form = document.createElement("div");
  form.className = "inline-comment-form";

  const ta = document.createElement("textarea");
  ta.className = "inline-comment-textarea";
  ta.placeholder = "Write a comment...";
  ta.rows = 3;
  ta.addEventListener("input", () => {
    ta.style.height = "auto";
    ta.style.height = Math.max(60, ta.scrollHeight) + "px";
  });

  const actions = document.createElement("div");
  actions.className = "inline-comment-actions";

  const cancelBtn = document.createElement("button");
  cancelBtn.className = "btn-cancel";
  cancelBtn.textContent = "Cancel";
  cancelBtn.addEventListener("click", () => formRow.remove());

  const addBtn = document.createElement("button");
  addBtn.className = "btn-add-comment";
  addBtn.textContent = "Add Comment";
  addBtn.addEventListener("click", async () => {
    const text = ta.value.trim();
    if (!text) { ta.focus(); return; }

    const localId = "local-" + getNextLocalId();
    let serverId = null;
    try {
      const data = await postComment({
        file_path: filePath,
        line_number: lineNumber,
        line_type: "context",
        line_content: lineContent,
        user_message: text,
      });
      serverId = data.id;
    } catch { /* will retry on submit */ }

    addComment({
      localId, serverId,
      filePath, lineNumber,
      lineType: "context",
      lineContent,
      userMessage: text,
      timestamp: new Date().toISOString(),
      status: "draft",
      replies: [],
      _anchorRow: row,
    });

    formRow.remove();
    renderFileInlineThreads();
    renderCommentSidebar();
  });

  ta.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      addBtn.click();
    }
  });

  actions.appendChild(cancelBtn);
  actions.appendChild(addBtn);
  form.appendChild(ta);
  form.appendChild(actions);
  formCell.appendChild(form);
  formRow.appendChild(formCell);

  row.after(formRow);
  ta.focus();
}

export function renderFileInlineThreads() {
  document.querySelectorAll(".file-view-wrapper .inline-comment-thread-row").forEach(el => el.remove());

  const grouped = new Map();
  comments.forEach(c => {
    const key = `${c.filePath}:${c.lineNumber}`;
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(c);
  });

  grouped.forEach((lineComments) => {
    const anchorRow = findFileAnchorRow(lineComments[0]);
    if (!anchorRow) return;

    const threadRow = document.createElement("tr");
    threadRow.className = "inline-comment-thread-row";

    const threadCell = document.createElement("td");
    threadCell.colSpan = 2;
    threadCell.className = "inline-comment-thread-cell";

    lineComments.forEach(c => {
      const thread = buildFileThread(c);
      threadCell.appendChild(thread);
    });

    threadRow.appendChild(threadCell);

    let insertAfter = anchorRow;
    while (insertAfter.nextElementSibling?.classList.contains("inline-comment-form-row")) {
      insertAfter = insertAfter.nextElementSibling;
    }
    insertAfter.after(threadRow);
  });
}

function findFileAnchorRow(comment) {
  if (comment._anchorRow && comment._anchorRow.isConnected) return comment._anchorRow;

  const wrappers = document.querySelectorAll(".file-view-wrapper");
  for (const wrapper of wrappers) {
    if (wrapper.dataset.filePath !== comment.filePath) continue;
    const row = wrapper.querySelector(`tr[data-line-num="${comment.lineNumber}"]`);
    if (row) {
      comment._anchorRow = row;
      return row;
    }
  }
  return null;
}

function buildFileThread(comment) {
  const thread = document.createElement("div");
  thread.className = `inline-thread ${comment.status}`;
  thread.dataset.localId = comment.localId;

  const header = document.createElement("div");
  header.className = "inline-thread-header";

  const statusBadge = document.createElement("span");
  statusBadge.className = `status-badge ${comment.status}`;
  statusBadge.textContent = comment.status;
  header.appendChild(statusBadge);

  const fileLine = document.createElement("span");
  fileLine.className = "inline-thread-location";
  fileLine.textContent = `Line ${comment.lineNumber}`;
  header.appendChild(fileLine);

  thread.appendChild(header);

  const body = document.createElement("div");
  body.className = "inline-thread-body";
  body.textContent = comment.userMessage;
  thread.appendChild(body);

  if (comment.replies && comment.replies.length > 0) {
    const repliesEl = document.createElement("div");
    repliesEl.className = "inline-thread-replies";
    comment.replies.forEach(r => {
      const reply = document.createElement("div");
      reply.className = `inline-reply ${r.author}`;

      const author = document.createElement("span");
      author.className = "inline-reply-author";
      author.textContent = r.author === "claude" ? "Claude" : "You";
      reply.appendChild(author);

      const msg = document.createElement("div");
      msg.className = "inline-reply-message";
      if (r.author === "claude") {
        // Safe: DOMPurify.sanitize() strips any XSS vectors before DOM insertion
        const sanitized = DOMPurify.sanitize(marked.parse(r.message));
        msg.innerHTML = sanitized; // nosec: DOMPurify-sanitized
      } else {
        msg.textContent = r.message;
      }
      reply.appendChild(msg);
      repliesEl.appendChild(reply);
    });
    thread.appendChild(repliesEl);
  }

  const replyTrigger = document.createElement("span");
  replyTrigger.className = "inline-reply-trigger";
  replyTrigger.textContent = comment.status === "resolved" ? "Reply (reopens)" : "Reply";
  replyTrigger.addEventListener("click", () => showReplyInput(thread, comment));
  thread.appendChild(replyTrigger);

  return thread;
}

function showReplyInput(threadEl, comment) {
  if (threadEl.querySelector(".inline-reply-input-area")) return;

  const area = document.createElement("div");
  area.className = "inline-reply-input-area";

  const ta = document.createElement("textarea");
  ta.className = "inline-reply-textarea";
  ta.placeholder = "Reply...";
  ta.rows = 2;

  const actions = document.createElement("div");
  actions.className = "inline-reply-actions";

  const cancelBtn = document.createElement("button");
  cancelBtn.className = "btn-cancel btn-sm";
  cancelBtn.textContent = "Cancel";
  cancelBtn.addEventListener("click", () => area.remove());

  const sendBtn = document.createElement("button");
  sendBtn.className = "btn-add-comment btn-sm";
  sendBtn.textContent = "Reply";
  sendBtn.addEventListener("click", async () => {
    const text = ta.value.trim();
    if (!text || !comment.serverId) return;
    const { postReply } = await import('./api.js');
    await postReply(comment.serverId, { message: text });
  });

  ta.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      sendBtn.click();
    }
  });

  actions.appendChild(cancelBtn);
  actions.appendChild(sendBtn);
  area.appendChild(ta);
  area.appendChild(actions);
  threadEl.appendChild(area);
  ta.focus();
}

export function buildFileSidebar() {
  const treeEl = document.getElementById("file-tree");
  treeEl.textContent = "";

  const wrappers = document.querySelectorAll(".file-view-wrapper");
  wrappers.forEach(wrapper => {
    const filePath = wrapper.dataset.filePath;
    const fileEl = document.createElement("div");
    fileEl.className = "file-tree-file";
    fileEl.style.paddingLeft = "10px";

    const name = document.createElement("span");
    name.className = "file-tree-file-name";
    name.textContent = filePath;
    name.title = filePath;
    fileEl.appendChild(name);

    fileEl.addEventListener("click", () => {
      document.querySelectorAll(".file-tree-file").forEach(el => el.classList.remove("active"));
      fileEl.classList.add("active");
      wrapper.scrollIntoView({ behavior: "smooth", block: "start" });
    });

    treeEl.appendChild(fileEl);
  });
}

export async function loadExistingFileComments() {
  const serverComments = await fetchAllComments();
  serverComments.forEach(sc => {
    const existing = comments.find(c => c.serverId === sc.id);
    if (existing) {
      existing.status = sc.status;
      existing.replies = sc.replies || [];
    } else {
      addComment({
        localId: "server-" + sc.id,
        serverId: sc.id,
        filePath: sc.file_path,
        lineNumber: sc.line_number,
        lineType: sc.line_type,
        lineContent: sc.line_content,
        userMessage: sc.user_message,
        timestamp: sc.timestamp,
        status: sc.status,
        replies: sc.replies || [],
      });
    }
  });
}
