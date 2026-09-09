// Local to the overview: do not change timing text elsewhere in the console.
export function formatOverviewDuration(milliseconds) {
  if (milliseconds === null || milliseconds === undefined || milliseconds === '') return '—';
  const ms = Number(milliseconds);
  if (!Number.isFinite(ms) || ms < 0) return '—';
  if (ms > 0 && ms < 1000) return '不到 1 秒';
  let seconds = Math.floor(ms / 1000);
  const parts = [];
  for (const [unit, size] of [['天', 86400], ['小时', 3600], ['分', 60], ['秒', 1]]) {
    const value = Math.floor(seconds / size);
    if (value) parts.push(`${value} ${unit}`);
    seconds %= size;
  }
  return parts.join(' ') || '0 秒';
}
function clearElapsed(el) {
  if (el._overviewElapsedTimer) clearInterval(el._overviewElapsedTimer);
  el._overviewElapsedTimer = null;
  el._overviewElapsedSignature = '';
}
function bindElapsed(el, binding) {
  const {startAt, active, fallback = '—'} = binding.value || {};
  const signature = `${startAt}:${active}:${fallback}`;
  if (el._overviewElapsedSignature === signature) return;
  clearElapsed(el);
  el._overviewElapsedSignature = signature;
  const paint = () => {el.textContent = active && Number(startAt) > 0 ? formatOverviewDuration(Math.max(0, Date.now() - Number(startAt))) : fallback;};
  paint();
  if (active && Number(startAt) > 0) el._overviewElapsedTimer = setInterval(paint, 1000);
}
export const vOverviewElapsed = {mounted:bindElapsed,updated:bindElapsed,beforeUnmount:clearElapsed};
