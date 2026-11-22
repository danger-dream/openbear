// Taking the user back to where a page assistant task started: the page by its path, then the
// object, which its page opens when it sees the request (the page may mount after the request).
import { ref } from 'vue';

export const openRequest = ref(null);
let count = 0;

// The page announces it has handled the request, so a later mount does not open it again.
export function takeOpenRequest(kind) {
  const request = openRequest.value;
  if (!request || request.kind !== kind) return null;
  openRequest.value = null;
  return request;
}

export function goToScope(scope) {
  if (!scope) return;
  if (scope.open?.kind === 'note') {
    window.dispatchEvent(new CustomEvent('openbear:open-kb-note', {detail: {noteUuid: scope.open.id}}));
    return;
  }
  if (scope.open) openRequest.value = {kind: scope.open.kind, id: scope.open.id, nonce: ++count};
  if (scope.path) window.dispatchEvent(new CustomEvent('openbear:open-page', {detail: {path: scope.path}}));
}
