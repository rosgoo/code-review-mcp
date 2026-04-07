// Client-side state store

export let currentMode = "empty"; // "diff", "files", "empty"
export let diffData = { diff: "", title: "" };
export let filesData = { files: [], title: "" };
export let comments = [];
export let viewMode = "line-by-line"; // or "side-by-side"
let nextLocalId = 1;

export function setMode(mode) {
  currentMode = mode;
}

export function setDiffData(data) {
  diffData = data;
}

export function setFilesData(data) {
  filesData = data;
}

export function setViewMode(mode) {
  viewMode = mode;
}

export function addComment(comment) {
  comments.push(comment);
}

export function removeComment(localId) {
  const idx = comments.findIndex(c => c.localId === localId);
  if (idx !== -1) comments.splice(idx, 1);
}

export function findComment(localId) {
  return comments.find(c => c.localId === localId);
}

export function findCommentByServerId(serverId) {
  return comments.find(c => c.serverId === serverId);
}

export function getNextLocalId() {
  return nextLocalId++;
}
