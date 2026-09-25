import {visibilityOperationId} from './messageVisibility.js';

const stateKey = Symbol('message-long-press');
const interactive = 'a, button, input, textarea, select, [contenteditable="true"], .message-visibility-action';

// A hold owns only the message gesture: taps, pans, pinches and inner controls
// keep their normal behavior. Global listeners are limited to the hold and its
// short compatibility-mouse release, then removed.
export function bindMessageLongPress(el, read, viewport = window) {
  let timer = null, pointer = null, suppressClickUntil = 0, releasePoint = null, releaseTimer = null;
  const eligible = () => {
    const {target, visibility} = read();
    return viewport.matchMedia('(max-width: 760px)').matches && !visibility.selecting.value && !visibility.busy.value && visibility.canTarget(target) && !visibility.isHidden(target);
  };
  function cancel() {
    if (timer !== null) viewport.clearTimeout(timer);
    timer = null; pointer = null;
    viewport.removeEventListener('pointermove', move, true);
    viewport.removeEventListener('pointerup', cancel, true);
    viewport.removeEventListener('pointercancel', cancel, true);
    viewport.removeEventListener('pointerdown', additionalPointer, true);
    viewport.removeEventListener('scroll', cancel, true);
  }
  function move(event) {
    if (!pointer || event.pointerId !== pointer.id) return;
    if (Math.hypot(event.clientX - pointer.x, event.clientY - pointer.y) > 8) cancel();
  }
  function additionalPointer(event) { if (pointer && event.pointerId !== pointer.id) cancel(); }
  function down(event) {
    cancel();
    if (!['touch', 'pen'].includes(event.pointerType) || event.isPrimary === false || event.button > 0 || !eligible() || event.target.closest?.(interactive)) return;
    const targetId = visibilityOperationId(read().target);
    pointer = {id: event.pointerId, x: event.clientX, y: event.clientY};
    viewport.addEventListener('pointermove', move, {capture: true, passive: true});
    viewport.addEventListener('pointerup', cancel, true);
    viewport.addEventListener('pointercancel', cancel, true);
    viewport.addEventListener('pointerdown', additionalPointer, true);
    viewport.addEventListener('scroll', cancel, {capture: true, passive: true});
    timer = viewport.setTimeout(() => {
      const valid = el.isConnected && eligible() && visibilityOperationId(read().target) === targetId;
      const point = pointer;
      cancel();
      if (!valid) return;
      clearReleaseGuard();
      releasePoint = point;
      suppressClickUntil = Date.now() + 800;
      // A drawer retargets the touch's compatibility mouse events to its
      // backdrop. Consume that release there, not just on the original row.
      for (const type of ['mousedown', 'mouseup', 'click']) viewport.addEventListener(type, click, true);
      releaseTimer = viewport.setTimeout(clearReleaseGuard, 800);
      const {target, turn, visibility} = read();
      visibility.openMobileMenu(target, turn);
    }, 450);
  }
  function contextMenu(event) {
    if ((pointer || Date.now() < suppressClickUntil) && !event.target.closest?.(interactive)) event.preventDefault();
  }
  function clearReleaseGuard() {
    if (releaseTimer !== null) viewport.clearTimeout(releaseTimer);
    releaseTimer = null; releasePoint = null; suppressClickUntil = 0;
    for (const type of ['mousedown', 'mouseup', 'click']) viewport.removeEventListener(type, click, true);
  }
  function click(event) {
    if (!releasePoint || Date.now() >= suppressClickUntil) return clearReleaseGuard();
    if (Math.hypot(event.clientX - releasePoint.x, event.clientY - releasePoint.y) > 12) return;
    event.preventDefault(); event.stopImmediatePropagation();
    if (event.type === 'click') clearReleaseGuard();
  }
  el.addEventListener('pointerdown', down, {passive: true});
  el.addEventListener('contextmenu', contextMenu);
  return () => {
    cancel(); clearReleaseGuard();
    el.removeEventListener('pointerdown', down);
    el.removeEventListener('contextmenu', contextMenu);
  };
}

export const vMessageLongPress = {
  mounted(el, binding) {
    const state = {value: binding.value};
    state.dispose = bindMessageLongPress(el, () => state.value);
    el[stateKey] = state;
  },
  updated(el, binding) { el[stateKey].value = binding.value; },
  beforeUnmount(el) { el[stateKey]?.dispose(); delete el[stateKey]; },
};
