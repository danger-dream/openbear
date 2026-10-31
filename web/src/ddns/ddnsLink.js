import { ref } from 'vue';

// A request to show 动态域名, the servers page's tab: {serverId, recordId, add, nonce}. Set by a link
// (/ddns?server=&record=&add=1, a server's card) before or after the servers page is up; the page
// takes it whenever it changes.
export const ddnsRequest = ref(null);
let count = 0;

export function requestDdns({serverId = '', recordId = '', add = false} = {}) {
  ddnsRequest.value = {serverId: String(serverId || ''), recordId: String(recordId || ''), add: Boolean(add), nonce: ++count};
}
