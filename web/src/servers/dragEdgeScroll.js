// While an item is dragged, scroll the nearest scrollable box when the pointer
// nears its top or bottom, and faster once the pointer is past the edge.
// Sortable only scrolls while the pointer is inside, within a few pixels of
// the edge, so dragging "to the bottom" past a long list did nothing.
const EDGE = 48;
const MAX_STEP = 22;

function scroller(el) {
  for (let node = el; node && node !== document.body; node = node.parentElement) {
    const style = getComputedStyle(node);
    if (/(auto|scroll)/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 1) return node;
  }
  return document.scrollingElement;
}

export function dragEdgeScroll(from) {
  if (typeof window === 'undefined' || !from) return () => {};
  const box = scroller(from);
  let x = null, y = null;
  const track = event => {
    const point = event.touches ? event.touches[0] : event;
    if (point && point.clientY) { x = point.clientX; y = point.clientY; }
  };
  // Chrome reports the pointer on drag and dragover; Firefox only on dragover
  // (its drag events carry zero coordinates, which track() skips).
  const events = ['drag', 'dragover', 'touchmove', 'pointermove', 'mousemove'];
  for (const name of events) document.addEventListener(name, track, {capture: true, passive: true});
  const timer = setInterval(() => {
    if (y === null) return;
    const rect = box === document.scrollingElement ? {top: 0, bottom: window.innerHeight, left: 0, right: window.innerWidth} : box.getBoundingClientRect();
    if (x < rect.left - EDGE || x > rect.right + EDGE) return;
    let step = 0;
    if (y > rect.bottom - EDGE) step = Math.min(1, (y - (rect.bottom - EDGE)) / (EDGE * 2));
    else if (y < rect.top + EDGE) step = -Math.min(1, ((rect.top + EDGE) - y) / (EDGE * 2));
    if (step) box.scrollTop += Math.sign(step) * Math.max(2, Math.round(Math.abs(step) * MAX_STEP));
  }, 16);
  return () => {
    for (const name of events) document.removeEventListener(name, track, {capture: true});
    clearInterval(timer);
  };
}
