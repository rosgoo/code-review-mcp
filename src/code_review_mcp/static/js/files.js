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

    const hasAnnotations = (file.added_lines && file.added_lines.length > 0) ||
                           (file.deleted_lines && file.deleted_lines.length > 0);
    const addedSet = new Set(file.added_lines || []);
    const deletedContent = file.deleted_content || {};
    const isMarkdown = file.language === "markdown" && !hasAnnotations;

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

    if (hasAnnotations) {
      const stats = document.createElement("span");
      stats.className = "file-view-stats";
      const adds = (file.added_lines || []).length;
      const dels = Object.values(deletedContent).reduce((n, arr) => n + arr.length, 0);
      if (adds > 0) {
        const s = document.createElement("span");
        s.className = "file-stat-add";
        s.textContent = `+${adds}`;
        stats.appendChild(s);
      }
      if (dels > 0) {
        const s = document.createElement("span");
        s.className = "file-stat-del";
        s.textContent = `-${dels}`;
        stats.appendChild(s);
      }
      header.appendChild(stats);
    } else {
      const lineCount = document.createElement("span");
      lineCount.className = "file-view-line-count";
      const lines = file.content.split("\n");
      lineCount.textContent = `${lines.length} lines`;
      header.appendChild(lineCount);
    }

    // Markdown source/rendered toggle
    let mdToggleBtn = null;
    if (isMarkdown) {
      mdToggleBtn = document.createElement("button");
      mdToggleBtn.className = "md-view-toggle";
      mdToggleBtn.type = "button";
      mdToggleBtn.textContent = "Source";
      mdToggleBtn.title = "Show source (for line-level commenting)";
      header.appendChild(mdToggleBtn);
    }

    wrapper.appendChild(header);

    // Markdown rendered body
    let mdBody = null;
    if (isMarkdown) {
      mdBody = document.createElement("div");
      mdBody.className = "file-view-markdown markdown-body";
      // Safe: DOMPurify.sanitize() strips XSS vectors before insertion
      const sanitized = DOMPurify.sanitize(marked.parse(file.content));
      mdBody.innerHTML = sanitized; // nosec: DOMPurify-sanitized
      wrapper.appendChild(mdBody);
    }

    // File body (source view — hidden initially for markdown)
    const body = document.createElement("div");
    body.className = "file-view-body";
    if (isMarkdown) body.style.display = "none";

    const table = document.createElement("table");
    table.className = "file-view-table";
    const tbody = document.createElement("tbody");

    const lines = file.content.split("\n");

    lines.forEach((line, i) => {
      const lineNum = i + 1;

      // Insert deleted lines before this line (if any)
      const delsHere = deletedContent[String(lineNum)];
      if (delsHere) {
        delsHere.forEach(delLine => {
          const delTr = document.createElement("tr");
          delTr.className = "file-view-line line-deleted";

          const delNumTd = document.createElement("td");
          delNumTd.className = "file-view-num del-num";
          delTr.appendChild(delNumTd);

          const delCodeTd = document.createElement("td");
          delCodeTd.className = "file-view-code";
          const delPre = document.createElement("pre");
          const delCode = document.createElement("code");
          delCode.className = file.language ? `language-${file.language}` : "";
          delCode.textContent = delLine || " ";
          delPre.appendChild(delCode);
          delCodeTd.appendChild(delPre);
          delTr.appendChild(delCodeTd);

          tbody.appendChild(delTr);
        });
      }

      const tr = document.createElement("tr");
      tr.className = "file-view-line";
      tr.dataset.lineNum = lineNum;

      if (addedSet.has(lineNum)) {
        tr.classList.add("line-added");
      }

      // Line number cell
      const numTd = document.createElement("td");
      numTd.className = "file-view-num";
      numTd.textContent = lineNum;
      numTd.style.position = "relative";

      const addBtn = document.createElement("button");
      addBtn.className = "line-add-btn";
      addBtn.textContent = "+";
      addBtn.title = "Add comment";
      addBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        openFileCommentForm(tr, file.path, lineNum, line);
      });
      numTd.appendChild(addBtn);
      tr.appendChild(numTd);

      // Code cell
      const codeTd = document.createElement("td");
      codeTd.className = "file-view-code";
      const codePre = document.createElement("pre");
      const codeEl = document.createElement("code");
      codeEl.className = file.language ? `language-${file.language}` : "";
      codeEl.textContent = line || " ";
      codePre.appendChild(codeEl);
      codeTd.appendChild(codePre);
      tr.appendChild(codeTd);

      tbody.appendChild(tr);
    });

    table.appendChild(tbody);
    body.appendChild(table);
    wrapper.appendChild(body);
    container.appendChild(wrapper);

    // Syntax highlighting
    if (window.hljs) {
      wrapper.querySelectorAll("code[class^='language-']").forEach(block => {
        hljs.highlightElement(block);
      });
    }

    // Collapse unchanged regions (only for annotated files)
    if (hasAnnotations) {
      collapseUnchangedRegions(tbody, addedSet, deletedContent);
    }

    // Wire markdown source/rendered toggle
    if (mdToggleBtn && mdBody) {
      mdToggleBtn.addEventListener("click", () => {
        const showingSource = body.style.display !== "none";
        const nextShowingSource = !showingSource;
        mdBody.style.display = nextShowingSource ? "none" : "";
        body.style.display = nextShowingSource ? "" : "none";
        mdToggleBtn.textContent = nextShowingSource ? "Rendered" : "Source";
        mdToggleBtn.title = nextShowingSource
          ? "Switch back to rendered markdown"
          : "Show source (for line-level commenting)";
        wrapper.dataset.userToggled = "1";
      });
    }
  });
}

// ── Collapse unchanged regions ──────────────────────────────────────────────

const CONTEXT_PADDING = 4; // lines of context to keep around changes
const MIN_COLLAPSIBLE = 5; // minimum lines to bother collapsing

function collapseUnchangedRegions(tbody, addedSet, deletedContent) {
  const rows = Array.from(tbody.querySelectorAll("tr.file-view-line"));

  // Mark each row as "changed" or not
  const isChanged = rows.map(row => {
    if (row.classList.contains("line-added") || row.classList.contains("line-deleted")) return true;
    const lineNum = parseInt(row.dataset.lineNum, 10);
    // Check if deleted content appears right before or after this line
    if (deletedContent[String(lineNum)] || deletedContent[String(lineNum + 1)]) return true;
    return false;
  });

  // Compute distance to nearest changed line
  const dist = new Array(rows.length).fill(Infinity);
  let lastChanged = -Infinity;
  for (let i = 0; i < rows.length; i++) {
    if (isChanged[i]) lastChanged = i;
    dist[i] = i - lastChanged;
  }
  lastChanged = Infinity;
  for (let i = rows.length - 1; i >= 0; i--) {
    if (isChanged[i]) lastChanged = i;
    dist[i] = Math.min(dist[i], lastChanged - i);
  }

  // Collapse runs of unchanged lines far from changes
  let i = 0;
  while (i < rows.length) {
    if (dist[i] > CONTEXT_PADDING && !isChanged[i]) {
      const start = i;
      while (i < rows.length && dist[i] > CONTEXT_PADDING && !isChanged[i]) i++;
      const end = i;
      const count = end - start;

      if (count >= MIN_COLLAPSIBLE) {
        for (let j = start; j < end; j++) {
          rows[j].classList.add("collapsed-context");
          rows[j].style.display = "none";
        }

        const expandRow = document.createElement("tr");
        expandRow.className = "context-expand-row";
        const expandCell = document.createElement("td");
        expandCell.colSpan = 2;
        expandCell.className = "context-expand-cell";

        const btn = document.createElement("button");
        btn.className = "context-expand-btn";
        btn.textContent = `Show ${count} hidden lines`;
        btn.addEventListener("click", () => {
          for (let j = start; j < end; j++) {
            rows[j].style.display = "";
            rows[j].classList.remove("collapsed-context");
          }
          expandRow.remove();
        });

        expandCell.appendChild(btn);
        expandRow.appendChild(expandCell);
        rows[start].before(expandRow);
      }
    } else {
      i++;
    }
  }
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

  // For markdown files that have comments and the user hasn't manually toggled,
  // force source view so comment threads anchor visibly.
  const filesWithComments = new Set(comments.map(c => c.filePath));
  document.querySelectorAll(".file-view-wrapper").forEach(wrapper => {
    if (wrapper.dataset.userToggled === "1") return;
    const mdBody = wrapper.querySelector(".file-view-markdown");
    const sourceBody = wrapper.querySelector(".file-view-body");
    const toggle = wrapper.querySelector(".md-view-toggle");
    if (!mdBody || !sourceBody || !toggle) return;
    if (filesWithComments.has(wrapper.dataset.filePath) && sourceBody.style.display === "none") {
      mdBody.style.display = "none";
      sourceBody.style.display = "";
      toggle.textContent = "Rendered";
      toggle.title = "Switch back to rendered markdown";
    }
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
  const files = [];
  wrappers.forEach(wrapper => {
    files.push({ path: wrapper.dataset.filePath, wrapper });
  });

  // Build directory tree
  const tree = {};
  files.forEach(f => {
    const parts = f.path.split("/");
    let node = tree;
    parts.forEach((part, i) => {
      if (i === parts.length - 1) {
        if (!node._files) node._files = [];
        node._files.push({ name: part, ...f });
      } else {
        if (!node[part]) node[part] = {};
        node = node[part];
      }
    });
  });

  // Collapse single-child directory chains
  function getCollapsedDirName(node, name) {
    const dirs = Object.keys(node).filter(k => k !== "_files");
    const hasFiles = node._files && node._files.length > 0;
    if (dirs.length === 1 && !hasFiles) {
      return getCollapsedDirName(node[dirs[0]], name + "/" + dirs[0]);
    }
    return { name, node };
  }

  function renderTreeNode(node, parentEl, depth) {
    const dirs = Object.keys(node).filter(k => k !== "_files").sort();
    dirs.forEach(dirName => {
      const { name: collapsedName, node: collapsedNode } = getCollapsedDirName(node[dirName], dirName);

      const dirEl = document.createElement("div");
      dirEl.className = "file-tree-dir";

      const label = document.createElement("div");
      label.className = "file-tree-dir-label";
      label.style.paddingLeft = (10 + depth * 16) + "px";

      const toggle = document.createElement("span");
      toggle.className = "file-tree-dir-toggle";
      toggle.textContent = "\u203A";
      label.appendChild(toggle);

      const name = document.createElement("span");
      name.className = "file-tree-dir-name";
      name.textContent = collapsedName;
      name.title = collapsedName;
      label.appendChild(name);

      label.addEventListener("click", () => dirEl.classList.toggle("collapsed"));

      dirEl.appendChild(label);

      const children = document.createElement("div");
      children.className = "file-tree-dir-children";
      renderTreeNode(collapsedNode, children, depth + 1);
      dirEl.appendChild(children);

      parentEl.appendChild(dirEl);
    });

    if (node._files) {
      node._files.forEach(f => {
        const fileEl = document.createElement("div");
        fileEl.className = "file-tree-file";
        fileEl.style.paddingLeft = (10 + (depth + (dirs.length > 0 ? 1 : 0)) * 16) + "px";

        const name = document.createElement("span");
        name.className = "file-tree-file-name";
        name.textContent = f.name;
        name.title = f.path;
        fileEl.appendChild(name);

        fileEl.addEventListener("click", () => {
          document.querySelectorAll(".file-tree-file").forEach(el => el.classList.remove("active"));
          fileEl.classList.add("active");
          f.wrapper.scrollIntoView({ behavior: "smooth", block: "start" });
        });

        parentEl.appendChild(fileEl);
      });
    }
  }

  renderTreeNode(tree, treeEl, 0);

  // Init filter
  const filterInput = document.getElementById("file-filter");
  if (filterInput && !filterInput._fileBound) {
    filterInput._fileBound = true;
    filterInput.addEventListener("input", () => {
      const filter = filterInput.value.toLowerCase();
      treeEl.querySelectorAll(".file-tree-file").forEach(el => {
        const path = el.querySelector(".file-tree-file-name")?.title || "";
        el.style.display = path.toLowerCase().includes(filter) ? "" : "none";
      });
      // Show/hide dirs based on whether they have visible children
      treeEl.querySelectorAll(".file-tree-dir").forEach(dir => {
        const visibleFiles = dir.querySelectorAll(".file-tree-file:not([style*='display: none'])");
        dir.style.display = visibleFiles.length > 0 ? "" : "none";
      });
    });
  }
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
