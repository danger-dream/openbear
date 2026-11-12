import { onBeforeUnmount, onMounted, ref } from 'vue';

// Whether a section is on or near the screen of the pane it scrolls in: the detail page's sections
// read their data the first time they come near, and refresh only while they are there.
export function useVisible(target, {margin = 300} = {}) {
  const visible = ref(false), seen = ref(false);
  let observer = null;
  onMounted(() => {
    const el = target.value;
    if (!el || typeof IntersectionObserver === 'undefined') { visible.value = seen.value = true; return; }
    observer = new IntersectionObserver(entries => {
      for (const entry of entries) {
        visible.value = entry.isIntersecting;
        if (entry.isIntersecting) seen.value = true;
      }
    }, {root: el.closest('.sv-scroll'), rootMargin: `${margin}px 0px`});
    observer.observe(el);
  });
  onBeforeUnmount(() => observer?.disconnect());
  return {visible, seen};
}

// Calls fn every `ms` while `on()` holds and the browser tab is in front; returns the stop.
export function every(ms, on, fn) {
  const timer = setInterval(() => { if (!document.hidden && on()) fn(); }, ms);
  return () => clearInterval(timer);
}
