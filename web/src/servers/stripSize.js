// One ResizeObserver for every latency strip. Each is told the whole device pixels its minutes may use
// (px), the ratio, and where its drawing starts: `offset` CSS pixels in, on a device pixel, one pixel
// before the first minute. Chromium may draw an SVG's content a device pixel to either side of where it
// was put, and cuts it at the SVG's edge; that pixel at each end keeps every minute whole. The first
// answer comes after layout and before the frame is painted.
const handlers = new WeakMap();
let observer = null;

function measured(entry) {
  const dpr = window.devicePixelRatio || 1, box = entry.target.getBoundingClientRect();
  const start = Math.ceil(box.left * dpr - 0.01) + 1, end = Math.floor(box.right * dpr + 0.01) - 1;
  return {px: Math.max(0, end - start), dpr, offset: +((start - 1) / dpr - box.left).toFixed(4)};
}

export function watchSize(el, handler) {
  if (!el || typeof ResizeObserver === 'undefined') return;
  observer ||= new ResizeObserver(entries => { for (const e of entries) handlers.get(e.target)?.(measured(e)); });
  handlers.set(el, handler);
  observer.observe(el);
}

export function unwatchSize(el) {
  if (!el || !observer) return;
  observer.unobserve(el);
  handlers.delete(el);
}
