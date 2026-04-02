// Diff rendering with diff2html + inline commenting + file tree + multi-line selection

import { diffData, setDiffData, comments, addComment, getNextLocalId, viewMode, findComment } from './state.js';
import { fetchDiff, fetchAllComments, postComment } from './api.js';
import { renderCommentSidebar } from './comments.js';

// Track multi-line selection state
let selectionStart = null; // { row, info }
let selectionEnd = null;
let isSelecting = false;

// ── Render diff ────────────────────────────────────────────────────────────

export async function renderDiff() {
  const data = await fetchDiff();
  setDiffData(data);

  document.getElementById("review-title").textContent = data.title || "Code Review";
  document.getElementById("page-title").textContent = data.title || "Code Review";

  const container = document.getElementById("diff-content");
  container.textContent = "";

  if (!data.diff) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No diff loaded";
    container.appendChild(empty);
    return;
  }

  const targetEl = document.createElement("div");
  targetEl.id = "diff-target";
  container.appendChild(targetEl);

  const configuration = {
    drawFileList: false,
    matching: "lines",
    outputFormat: viewMode,
    highlight: true,
    fileListToggle: false,
    fileListStartVisible: false,
    fileContentToggle: true,
    stickyFileHeaders: true,
  };

  // diff2html renders its own sanitized output from diff text (not user HTML)
  const diff2htmlUi = new Diff2HtmlUI(targetEl, data.diff, configuration);
  diff2htmlUi.draw();
  diff2htmlUi.highlightCode();

  collapseContextLines();
  attachLineHandlers();
  addExpandButtons();
  updateFileStats();
}

// ── Build file tree in sidebar ─────────────────────────────────────────────

// Cached file list for filtering
let _parsedFiles = [];

export function buildFileTree(filterText) {
  const treeEl = document.getElementById("file-tree");
  treeEl.textContent = "";

  const fileWrappers = document.querySelectorAll(".d2h-file-wrapper");

  // Parse files
  if (!filterText) {
    _parsedFiles = [];
    fileWrappers.forEach(wrapper => {
      const nameEl = wrapper.querySelector(".d2h-file-name");
      const filePath = nameEl?.textContent?.trim() || "unknown";
      const adds = wrapper.querySelectorAll(".d2h-ins").length;
      const dels = wrapper.querySelectorAll(".d2h-del").length;
      _parsedFiles.push({ path: filePath, adds, dels, wrapper });
    });
  }

  const filter = (filterText || "").toLowerCase();
  const files = filter
    ? _parsedFiles.filter(f => f.path.toLowerCase().includes(filter))
    : _parsedFiles;

  // Build raw tree
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

  // Collapse single-child directory chains:
  // If a dir has exactly 1 subdir and 0 files, merge into "parent/child"
  function collapseNode(node, prefix) {
    const dirs = Object.keys(node).filter(k => k !== "_files");
    const hasFiles = node._files && node._files.length > 0;

    if (dirs.length === 1 && !hasFiles) {
      const dir = dirs[0];
      const label = prefix ? prefix + "/" + dir : dir;
      return collapseNode(node[dir], label);
    }

    // This is a branching point — build a collapsed node
    const result = { _label: prefix || null };
    if (node._files) result._files = node._files;

    dirs.forEach(dir => {
      const child = collapseNode(node[dir], dir);
      result[child._label || dir] = child;
    });

    return result;
  }

  const collapsed = collapseNode(tree, null);

  function renderTreeNode(node, parentEl, depth) {
    const dirs = Object.keys(node).filter(k => k !== "_files" && k !== "_label").sort();

    dirs.forEach(dirLabel => {
      const dirEl = document.createElement("div");
      dirEl.className = "file-tree-dir";

      const label = document.createElement("div");
      label.className = "file-tree-dir-label";
      label.style.paddingLeft = (10 + depth * 16) + "px";

      const toggle = document.createElement("span");
      toggle.className = "file-tree-dir-toggle";
      toggle.textContent = "\u203A"; // ›
      label.appendChild(toggle);

      const name = document.createElement("span");
      name.className = "file-tree-dir-name";
      name.textContent = dirLabel;
      name.title = dirLabel;
      label.appendChild(name);

      label.addEventListener("click", () => {
        dirEl.classList.toggle("collapsed");
      });

      dirEl.appendChild(label);

      const children = document.createElement("div");
      children.className = "file-tree-dir-children";
      renderTreeNode(node[dirLabel], children, depth + 1);
      dirEl.appendChild(children);

      parentEl.appendChild(dirEl);
    });

    // Files
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

        // Line change stats
        const stats = document.createElement("span");
        stats.className = "file-tree-file-stats";
        if (f.adds > 0) {
          const addSpan = document.createElement("span");
          addSpan.className = "file-stat-add";
          addSpan.textContent = `+${f.adds}`;
          stats.appendChild(addSpan);
        }
        if (f.dels > 0) {
          const delSpan = document.createElement("span");
          delSpan.className = "file-stat-del";
          delSpan.textContent = `-${f.dels}`;
          stats.appendChild(delSpan);
        }
        // Colored block bar
        const total = f.adds + f.dels;
        if (total > 0) {
          const bar = document.createElement("span");
          bar.className = "file-stat-bar";
          const maxBlocks = 5;
          const addBlocks = Math.max(1, Math.round((f.adds / total) * maxBlocks));
          const delBlocks = maxBlocks - addBlocks;
          for (let i = 0; i < addBlocks; i++) {
            const b = document.createElement("span");
            b.className = "file-stat-block add";
            bar.appendChild(b);
          }
          for (let i = 0; i < delBlocks; i++) {
            const b = document.createElement("span");
            b.className = "file-stat-block del";
            bar.appendChild(b);
          }
          stats.appendChild(bar);
        }
        fileEl.appendChild(stats);

        fileEl.addEventListener("click", () => {
          document.querySelectorAll(".file-tree-file").forEach(el => el.classList.remove("active"));
          fileEl.classList.add("active");
          f.wrapper.scrollIntoView({ behavior: "smooth", block: "start" });
        });

        parentEl.appendChild(fileEl);
      });
    }
  }

  renderTreeNode(collapsed, treeEl, 0);

  // Init filter if not already
  const filterInput = document.getElementById("file-filter");
  if (filterInput && !filterInput._bound) {
    filterInput._bound = true;
    filterInput.addEventListener("input", () => {
      buildFileTree(filterInput.value);
    });
  }
}

function updateFileStats() {
  const statsEl = document.getElementById("file-stats");
  const wrappers = document.querySelectorAll(".d2h-file-wrapper");
  let totalAdds = 0, totalDels = 0;
  wrappers.forEach(w => {
    totalAdds += w.querySelectorAll(".d2h-ins").length;
    totalDels += w.querySelectorAll(".d2h-del").length;
  });
  statsEl.textContent = "";
  if (totalAdds > 0 || totalDels > 0) {
    const addSpan = document.createElement("span");
    addSpan.className = "stat-add";
    addSpan.textContent = `+${totalAdds}`;
    const delSpan = document.createElement("span");
    delSpan.className = "stat-del";
    delSpan.textContent = ` -${totalDels}`;
    statsEl.appendChild(addSpan);
    statsEl.appendChild(delSpan);
  }
}

// ── Collapse long context runs between changes ─────────────────────────────

const CONTEXT_PADDING = 3; // lines of context to keep visible around changes
const MIN_COLLAPSIBLE = 4; // minimum hidden lines to bother collapsing

function collapseContextLines() {
  const fileWrappers = document.querySelectorAll(".d2h-file-wrapper");

  fileWrappers.forEach(wrapper => {
    const tbody = wrapper.querySelector(".d2h-diff-tbody");
    if (!tbody) return;

    const rows = Array.from(tbody.querySelectorAll("tr"));
    if (rows.length === 0) return;

    // Tag each row as "change" or "context"
    const isChange = rows.map(row => {
      return !!(row.querySelector("td.d2h-ins") || row.querySelector("td.d2h-del"));
    });

    // For each row, compute distance to nearest change
    const dist = new Array(rows.length).fill(Infinity);
    // Forward pass
    let lastChange = -Infinity;
    for (let i = 0; i < rows.length; i++) {
      if (isChange[i]) lastChange = i;
      dist[i] = i - lastChange;
    }
    // Backward pass
    lastChange = Infinity;
    for (let i = rows.length - 1; i >= 0; i--) {
      if (isChange[i]) lastChange = i;
      dist[i] = Math.min(dist[i], lastChange - i);
    }

    // Find runs of context rows that are far from changes
    let i = 0;
    while (i < rows.length) {
      // Skip hunk info rows
      if (rows[i].querySelector(".d2h-info")) { i++; continue; }

      if (dist[i] > CONTEXT_PADDING && !isChange[i]) {
        // Start of a collapsible run
        const start = i;
        while (i < rows.length && dist[i] > CONTEXT_PADDING && !isChange[i] && !rows[i].querySelector(".d2h-info")) {
          i++;
        }
        const end = i; // exclusive
        const count = end - start;

        if (count >= MIN_COLLAPSIBLE) {
          // Hide these rows
          for (let j = start; j < end; j++) {
            rows[j].classList.add("collapsed-context");
            rows[j].style.display = "none";
          }

          // Insert an expand button row
          const expandRow = document.createElement("tr");
          expandRow.className = "context-expand-row";

          const expandCell = document.createElement("td");
          expandCell.colSpan = 20;
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

          // Insert before the first hidden row
          rows[start].before(expandRow);
        }
      } else {
        i++;
      }
    }
  });
}

// ── Add collapse/expand toggles to file headers ────────────────────────────

function addExpandButtons() {
  const fileWrappers = document.querySelectorAll(".d2h-file-wrapper");
  fileWrappers.forEach(wrapper => {
    const header = wrapper.querySelector(".d2h-file-header");
    if (!header || header.querySelector(".file-collapse-btn")) return;

    const btn = document.createElement("button");
    btn.className = "file-collapse-btn";
    btn.title = "Collapse file";

    const icon = document.createElement("span");
    icon.className = "file-collapse-icon";
    icon.textContent = "\u25BE"; // ▾
    btn.appendChild(icon);

    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const diffBody = wrapper.querySelector(".d2h-diff-table, .d2h-file-diff");
      if (!diffBody) return;

      const isCollapsed = wrapper.classList.toggle("file-collapsed");
      icon.textContent = isCollapsed ? "\u25B8" : "\u25BE"; // ▸ or ▾
      btn.title = isCollapsed ? "Expand file" : "Collapse file";
    });

    // Prepend to header
    header.style.position = "relative";
    header.insertBefore(btn, header.firstChild);
  });
}

// ── Load existing comments from server ─────────────────────────────────────

export async function loadExistingComments() {
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

// ── Attach click/drag handlers to diff lines ───────────────────────────────

function attachLineHandlers() {
  const rows = document.querySelectorAll(".d2h-diff-tbody tr");

  rows.forEach(row => {
    if (row.querySelector(".d2h-info") || row.querySelector(".d2h-file-header")) return;
    const lineNumCell = row.querySelector("td.d2h-code-linenumber");
    if (!lineNumCell) return;

    lineNumCell.style.position = "relative";
    lineNumCell.style.cursor = "pointer";

    // Click on line number to start comment (or start multi-line selection)
    lineNumCell.addEventListener("mousedown", (e) => {
      e.preventDefault();
      const info = getLineInfo(row);
      if (!info) return;
      selectionStart = { row, info };
      selectionEnd = { row, info };
      isSelecting = true;
      highlightSelection();
    });

    // Drag over line numbers extends selection
    lineNumCell.addEventListener("mouseenter", () => {
      if (!isSelecting) return;
      const info = getLineInfo(row);
      if (!info) return;
      if (info.filePath !== selectionStart.info.filePath) return; // same file only
      selectionEnd = { row, info };
      highlightSelection();
    });

    // Create the "+" add-comment button
    const addBtn = document.createElement("button");
    addBtn.className = "line-add-btn";
    addBtn.textContent = "+";
    addBtn.title = "Add comment";
    lineNumCell.appendChild(addBtn);

    addBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      e.preventDefault();
      openInlineCommentForm(row, row);
    });
  });

  // Finish multi-line selection on mouseup
  document.addEventListener("mouseup", () => {
    if (!isSelecting) return;
    isSelecting = false;
    if (selectionStart && selectionEnd) {
      openInlineCommentForm(selectionStart.row, selectionEnd.row);
    }
    clearSelectionHighlight();
    selectionStart = null;
    selectionEnd = null;
  });
}

function highlightSelection() {
  clearSelectionHighlight();
  if (!selectionStart || !selectionEnd) return;

  const rows = getRowsBetween(selectionStart.row, selectionEnd.row);
  rows.forEach(r => r.classList.add("line-selected"));
}

function clearSelectionHighlight() {
  document.querySelectorAll(".line-selected").forEach(r => r.classList.remove("line-selected"));
}

function getRowsBetween(startRow, endRow) {
  const allRows = Array.from(startRow.closest("tbody")?.querySelectorAll("tr") || []);
  const startIdx = allRows.indexOf(startRow);
  const endIdx = allRows.indexOf(endRow);
  if (startIdx === -1 || endIdx === -1) return [startRow];
  const [lo, hi] = startIdx <= endIdx ? [startIdx, endIdx] : [endIdx, startIdx];
  return allRows.slice(lo, hi + 1).filter(r => {
    return !r.querySelector(".d2h-info") && r.querySelector("td.d2h-code-linenumber");
  });
}

// ── Extract line info from a diff row ──────────────────────────────────────

function getLineInfo(row) {
  const lineNumCell = row.querySelector("td.d2h-code-linenumber");
  if (!lineNumCell) return null;

  const fileWrapper = row.closest(".d2h-file-wrapper");
  const fileHeader = fileWrapper?.querySelector(".d2h-file-name");
  const filePath = fileHeader?.textContent?.trim() || "unknown";

  const lineNum1 = lineNumCell.querySelector(".line-num1");
  const lineNum2 = lineNumCell.querySelector(".line-num2");
  const oldNum = lineNum1 ? parseInt(lineNum1.textContent, 10) : null;
  const newNum = lineNum2 ? parseInt(lineNum2.textContent, 10) : null;

  const codeCell = row.querySelector("td.d2h-ins, td.d2h-del, td.d2h-cntx");
  let lineType = "context";
  if (codeCell?.classList.contains("d2h-ins")) lineType = "add";
  else if (codeCell?.classList.contains("d2h-del")) lineType = "delete";

  const codeLine = row.querySelector(".d2h-code-line-ctn");
  const lineContent = codeLine?.textContent || "";

  const lineNumber = lineType === "delete" ? (oldNum || newNum || 0) : (newNum || oldNum || 0);

  return { filePath, lineNumber, lineType, lineContent, oldNum, newNum };
}

// ── Open inline comment form ───────────────────────────────────────────────

function openInlineCommentForm(startRow, endRow) {
  // Don't open duplicate forms
  const nextSibling = endRow.nextElementSibling;
  if (nextSibling?.classList.contains("inline-comment-form-row")) return;

  const startInfo = getLineInfo(startRow);
  const endInfo = getLineInfo(endRow);
  if (!startInfo) return;

  // Collect all selected lines for context
  const rows = getRowsBetween(startRow, endRow);
  const selectedLines = rows.map(r => {
    const info = getLineInfo(r);
    return info ? info.lineContent : "";
  }).filter(Boolean);

  // Use the first line's info for the comment anchor
  const lineInfo = startInfo;
  const isMultiLine = rows.length > 1;
  const lineRange = isMultiLine
    ? `L${startInfo.lineNumber}-L${endInfo?.lineNumber || startInfo.lineNumber}`
    : `L${startInfo.lineNumber}`;

  const formRow = document.createElement("tr");
  formRow.className = "inline-comment-form-row";

  const formCell = document.createElement("td");
  formCell.colSpan = 20;
  formCell.className = "inline-comment-form-cell";

  const form = document.createElement("div");
  form.className = "inline-comment-form";

  // Show selected line range
  if (isMultiLine) {
    const rangeLabel = document.createElement("div");
    rangeLabel.className = "inline-form-range";
    rangeLabel.textContent = `Commenting on ${lineRange} (${rows.length} lines)`;
    form.appendChild(rangeLabel);
  }

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
    const lineContent = isMultiLine ? selectedLines.join("\n") : lineInfo.lineContent;

    let serverId = null;
    try {
      const data = await postComment({
        file_path: lineInfo.filePath,
        line_number: lineInfo.lineNumber,
        line_type: lineInfo.lineType,
        line_content: lineContent,
        user_message: text,
      });
      serverId = data.id;
    } catch { /* will retry on submit */ }

    addComment({
      localId,
      serverId,
      filePath: lineInfo.filePath,
      lineNumber: lineInfo.lineNumber,
      lineType: lineInfo.lineType,
      lineContent: lineContent,
      userMessage: text,
      timestamp: new Date().toISOString(),
      status: "draft",
      replies: [],
      _anchorRow: startRow,
    });

    formRow.remove();
    renderInlineThreads();
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

  endRow.after(formRow);
  ta.focus();
}

// ── Render inline comment threads ──────────────────────────────────────────

export function renderInlineThreads() {
  document.querySelectorAll(".inline-comment-thread-row").forEach(el => el.remove());

  const grouped = new Map();
  comments.forEach(c => {
    const key = `${c.filePath}:${c.lineNumber}:${c.lineType}`;
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(c);
  });

  grouped.forEach((lineComments, _key) => {
    const anchorRow = findAnchorRow(lineComments[0]);
    if (!anchorRow) return;

    const threadRow = document.createElement("tr");
    threadRow.className = "inline-comment-thread-row";

    const threadCell = document.createElement("td");
    threadCell.colSpan = 20;
    threadCell.className = "inline-comment-thread-cell";

    lineComments.forEach(c => {
      const thread = buildInlineThread(c);
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

function findAnchorRow(comment) {
  if (comment._anchorRow && comment._anchorRow.isConnected) return comment._anchorRow;

  const fileWrappers = document.querySelectorAll(".d2h-file-wrapper");
  for (const wrapper of fileWrappers) {
    const fileHeader = wrapper.querySelector(".d2h-file-name");
    const filePath = fileHeader?.textContent?.trim();
    if (filePath !== comment.filePath) continue;

    const rows = wrapper.querySelectorAll(".d2h-diff-tbody tr");
    for (const row of rows) {
      const info = getLineInfo(row);
      if (!info) continue;
      if (info.lineNumber === comment.lineNumber && info.lineType === comment.lineType) {
        comment._anchorRow = row;
        return row;
      }
    }
  }
  return null;
}

function buildInlineThread(comment) {
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
        // Safe: sanitized with DOMPurify before DOM insertion
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
  replyTrigger.addEventListener("click", () => {
    showInlineReplyInput(thread, comment);
  });
  thread.appendChild(replyTrigger);

  return thread;
}

function showInlineReplyInput(threadEl, comment) {
  if (threadEl.querySelector(".inline-reply-input-area")) return;

  const area = document.createElement("div");
  area.className = "inline-reply-input-area";

  const ta = document.createElement("textarea");
  ta.className = "inline-reply-textarea";
  ta.placeholder = "Reply...";
  ta.rows = 2;
  ta.addEventListener("input", () => {
    ta.style.height = "auto";
    ta.style.height = Math.max(40, ta.scrollHeight) + "px";
  });

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
    if (!text) return;
    if (!comment.serverId) return;
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
