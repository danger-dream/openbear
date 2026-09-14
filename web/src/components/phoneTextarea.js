// Native textarea is the phone's momentum/IME owner. Flow mode grows inside an
// already-scrollable form, so the form never nests another text scroll region.
export function resizePhoneTextarea(element, flow) {
  if (!element || !element.clientWidth) return;
  if (!flow) { element.style.removeProperty('height'); return; }
  const scrolls = [];
  for (let parent = element.parentElement; parent; parent = parent.parentElement) {
    if (parent.scrollTop) scrolls.push([parent, parent.scrollTop]);
  }
  element.style.height = '0px';
  element.style.height = `${Math.max(200, element.scrollHeight + 2)}px`;
  for (const [parent, top] of scrolls) parent.scrollTop = top;
}
