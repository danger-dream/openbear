// Sortable's edge-scroll stops when the desktop drag cursor passes above a
// nested scroller's top edge (e.g. into the model header). Keep scrolling that
// same scroller while the cursor is above it, so the first row can be reached.
export function scrollModelListAbove(container) {
  if (!container || typeof window === "undefined" || !window.matchMedia("(min-width: 1024px)").matches) return () => {};
  let above = false;
  const onDragOver = event => {
    const rect = container.getBoundingClientRect();
    above = event.clientY >= 0 && event.clientY < rect.top && event.clientX >= rect.left && event.clientX <= rect.right;
  };
  document.addEventListener("dragover", onDragOver, true);
  const timer = setInterval(() => {
    if (above && container.scrollTop > 0) container.scrollTop = Math.max(0, container.scrollTop - 18);
  }, 24);
  return () => {
    document.removeEventListener("dragover", onDragOver, true);
    clearInterval(timer);
  };
}
