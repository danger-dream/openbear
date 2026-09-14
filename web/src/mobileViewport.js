// Layout-width media queries, not visualViewport.width: pinch zoom must never
// turn a desktop into a phone or continuously reflow the zoomed page.
export const MOBILE_VIEWPORT_QUERY = "(max-width: 760px), (hover: none) and (pointer: coarse)";
const ATTRIBUTE = "data-openbear-mobile-viewport";
const HEIGHT = "--mobile-viewport-height";
const TOP = "--mobile-viewport-top";

export function installMobileViewport({ window: win = globalThis.window, root = win?.document?.documentElement, beforeChange, afterChange } = {}) {
  if (!win?.matchMedia || !root) return () => {};
  const media = win.matchMedia(MOBILE_VIEWPORT_QUERY);
  const standalone = win.matchMedia("(display-mode: standalone)");
  const viewport = win.visualViewport;
  const listeners = [];
  let snapshot = null;
  let disposed = false;
  let frame = null;
  function listen(target, event, handler) {
    if (target?.addEventListener) {
      target.addEventListener(event, handler);
      listeners.push(() => target.removeEventListener(event, handler));
    } else if (event === "change" && target?.addListener) {
      target.addListener(handler);
      listeners.push(() => target.removeListener(handler));
    }
  }
  function restore() {
    if (!snapshot) return;
    for (const [key, value, priority] of snapshot.styles) {
      if (value) root.style.setProperty(key, value, priority);
      else root.style.removeProperty(key);
    }
    if (snapshot.attribute === null) root.removeAttribute(ATTRIBUTE);
    else root.setAttribute(ATTRIBUTE, snapshot.attribute);
    snapshot = null;
  }
  function sync() {
    frame = null;
    if (disposed) return;
    if (!media.matches) {
      if (snapshot) {
        const anchor = beforeChange?.();
        restore();
        afterChange?.(anchor);
      }
      return;
    }
    // Browser pinch zoom/pan remains native. Keep the unzoomed layout rather
    // than fitting it into the magnified viewport (which would undo zoom).
    if (viewport && Math.abs(Number(viewport.scale || 1) - 1) > 0.01) return;
    const height = Number(viewport?.height || win.innerHeight);
    if (!Number.isFinite(height) || height <= 0) return;
    const anchor = beforeChange?.();
    if (!snapshot) snapshot = {
      attribute: root.getAttribute(ATTRIBUTE),
      styles: [HEIGHT, TOP].map(key => [key, root.style.getPropertyValue(key), root.style.getPropertyPriority(key)]),
    };
    root.style.setProperty(HEIGHT, `${height}px`);
    root.style.setProperty(TOP, `${Math.max(0, Number(viewport?.offsetTop) || 0)}px`);
    root.setAttribute(ATTRIBUTE, "");
    // Also run for unchanged dimensions: env(safe-area-inset-*) can change on
    // rotation/standalone transitions independently of the visual viewport.
    afterChange?.(anchor);
  }
  function schedule() {
    if (disposed || frame !== null) return;
    frame = win.requestAnimationFrame(sync);
  }
  listen(media, "change", schedule);
  listen(standalone, "change", schedule);
  for (const event of ["resize", "orientationchange", "pageshow"]) listen(win, event, schedule);
  for (const event of ["resize", "scroll"]) listen(viewport, event, schedule);
  sync();
  return () => {
    if (disposed) return;
    disposed = true;
    if (frame !== null) win.cancelAnimationFrame(frame);
    listeners.forEach(remove => remove());
    restore();
  };
}
