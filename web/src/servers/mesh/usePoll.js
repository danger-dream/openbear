import { computed, getCurrentInstance, inject, isRef, onBeforeUnmount, watch } from 'vue';

// Whether the mesh is on screen: the mesh tab is the one shown and the servers page is not put away
// (KeepAlive). MeshView provides it; outside it (tests, a lone panel) the mesh counts as shown.
export const MESH_VISIBLE = 'meshVisible';
export const useMeshVisible = () => inject(MESH_VISIBLE, computed(() => true));

// One reading kept fresh while the mesh is shown: `fn` runs at once on run(), then again `interval`
// after it finished. `interval` is milliseconds or a function giving them; 0 (or false) stops: no run and
// nothing scheduled until run() is called again. Hidden browser tabs keep the rhythm without asking.
// While `active` is false nothing more is scheduled; when it turns true it runs at once.
// `fn(current)`: current() tells whether this run is still the latest (a later run() or the end of the
// component makes it stale), so a late answer does not overwrite a newer one.
export function usePoll(fn, {interval, active = useMeshVisible()} = {}) {
  const every = () => Number((typeof interval === 'function' ? interval() : interval) || 0);
  const shown = () => Boolean(isRef(active) ? active.value : typeof active === 'function' ? active() : active ?? true);
  let timer = null, round = 0, alive = true;
  function schedule() {
    clearTimeout(timer); timer = null;
    const ms = every();
    if (alive && ms > 0 && shown()) timer = setTimeout(run, ms);
  }
  async function run() {
    clearTimeout(timer); timer = null;
    const mine = ++round;
    if (!alive || !shown() || !every()) return;
    if (!document.hidden) {
      try { await fn(() => alive && mine === round); }
      catch { /* fn reports its own errors; the rhythm goes on */ }
    }
    if (mine === round) schedule();
  }
  function stop() { round++; clearTimeout(timer); timer = null; }
  watch(shown, now => { if (now) void run(); else { clearTimeout(timer); timer = null; } });
  if (getCurrentInstance()) onBeforeUnmount(() => { alive = false; stop(); });
  return {run, schedule, stop};
}
