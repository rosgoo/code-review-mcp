// Column resize handles for file sidebar and comment margin

const MIN_SIDEBAR = 160;
const MAX_SIDEBAR = 500;
const MIN_COMMENTS = 200;
const MAX_COMMENTS = 480;

export function initResizeHandles() {
  initHandle('resize-sidebar', {
    getEl: () => document.getElementById('file-sidebar'),
    getSize: (el) => el.offsetWidth,
    applySize: (el, size) => { el.style.width = size + 'px'; },
    // Dragging right = grow sidebar
    calcNew: (delta, startSize) => Math.min(MAX_SIDEBAR, Math.max(MIN_SIDEBAR, startSize + delta)),
  });

  initHandle('resize-comments', {
    getEl: () => document.getElementById('comment-margin'),
    getSize: (el) => el.offsetWidth,
    applySize: (el, size) => { el.style.width = size + 'px'; },
    // Dragging left = grow comments (inverted)
    calcNew: (delta, startSize) => Math.min(MAX_COMMENTS, Math.max(MIN_COMMENTS, startSize - delta)),
  });
}

function initHandle(handleId, config) {
  const handle = document.getElementById(handleId);
  if (!handle) return;

  let startX = 0;
  let startSize = 0;
  let targetEl = null;

  function onMouseDown(e) {
    e.preventDefault();
    e.stopPropagation();
    targetEl = config.getEl();
    if (!targetEl) return;
    startX = e.clientX;
    startSize = config.getSize(targetEl);

    document.body.classList.add('resizing');
    handle.classList.add('active');
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
  }

  function onMouseMove(e) {
    if (!targetEl) return;
    const delta = e.clientX - startX;
    const newSize = config.calcNew(delta, startSize);
    config.applySize(targetEl, newSize);
  }

  function onMouseUp() {
    targetEl = null;
    document.body.classList.remove('resizing');
    handle.classList.remove('active');
    document.removeEventListener('mousemove', onMouseMove);
    document.removeEventListener('mouseup', onMouseUp);
  }

  handle.addEventListener('mousedown', onMouseDown);
}
