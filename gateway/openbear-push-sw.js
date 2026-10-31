/* Notifications only. No fetch handler, offline cache, or API interception. */
self.addEventListener("install", event => event.waitUntil(self.skipWaiting()));
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

// Store only subscription ownership, never cookie/context or business content.
async function boundaryState(value) {
  if (!self.indexedDB) return {};
  const db = await new Promise((resolve, reject) => { const r = self.indexedDB.open('openbear.push.boundary', 1); r.onupgradeneeded = () => r.result.createObjectStore('state'); r.onsuccess = () => resolve(r.result); r.onerror = () => reject(r.error); });
  try { return await new Promise((resolve, reject) => { const tx = db.transaction('state', value ? 'readwrite' : 'readonly'); const r = value ? tx.objectStore('state').put(value, 'owner') : tx.objectStore('state').get('owner'); tx.oncomplete = () => resolve(value || r.result || {}); tx.onerror = () => reject(tx.error); }); } finally { db.close(); }
}
self.addEventListener('message', event => {
  if (event.data?.type !== 'openbear:push-boundary' || !event.source?.url || new URL(event.source.url).origin !== self.location.origin) return;
  event.waitUntil(boundaryState({gateway: event.data.gateway === true, instanceId: String(event.data.instanceId || '')}));
});
function notificationUrl(data) {
  const id = typeof data?.conversationUuid === "string" ? data.conversationUuid : "";
  return id ? `/chat?id=${encodeURIComponent(id)}` : "/settings?section=system-settings&domain=notifications";
}

self.addEventListener("push", event => {
  event.waitUntil((async () => {
    let data = {};
    try { data = event.data?.json() || {}; } catch { /* Still show a visible notification. */ }
    // Push subscriptions are userVisibleOnly: do not silently consume pushes in
    // the worker (notably iOS may revoke such subscriptions). Foreground quieting
    // happens on the server BEFORE sending, using a short-lived device presence.
    const boundary = await boundaryState().catch(() => ({gateway: true}));
    const gateway = boundary.gateway || Boolean(data.instanceId);
    const matched = !gateway || Boolean(data.instanceId && data.instanceId === boundary.instanceId);
    const target = gateway ? (data.instanceId ? `${notificationUrl(data)}${notificationUrl(data).includes('?') ? '&' : '?'}_ob_instance=${encodeURIComponent(data.instanceId)}` : '/login') : notificationUrl(data);
    await self.registration.showNotification("OpenBear", {
      body: !matched ? '有另一登录的通知，请先确认账号。' : typeof data.body === "string" ? data.body : "有新的任务动态，请打开查看",
      icon: "/icons/openbear-192.png",
      badge: "/icons/openbear-192.png",
      tag: typeof data.tag === "string" ? data.tag : "openbear-update",
      data: {url: target, ...(gateway ? {instanceId: data.instanceId || '', gateway: true} : {})},
    });
  })());
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  event.waitUntil((async () => {
    let target = new URL("/chat", self.location.origin).href;
    try {
      const candidate = new URL(event.notification.data?.url || "/chat", self.location.origin);
      if (candidate.origin === self.location.origin && !candidate.username && !candidate.password && ["/chat", "/settings", "/login"].includes(candidate.pathname)) target = candidate.href;
    } catch { /* A malformed old notification still opens a safe app page. */ }
    const boundary = await boundaryState().catch(() => ({gateway: true}));
    if (boundary.gateway || event.notification.data?.gateway) {
      const instanceId = event.notification.data?.instanceId;
      if (!instanceId) target = new URL('/login', self.location.origin).href;
      else { const bound = new URL(target); bound.searchParams.set('_ob_instance', instanceId); target = bound.href; }
    }
    const clients = await self.clients.matchAll({type: "window", includeUncontrolled: true});
    const exact = clients.find(client => client.url === target);
    // Reuse an existing application window through SPA navigation, not reload:
    // unsent drafts, attachments and an active reply must not be discarded.
    const client = target.endsWith('/login') ? null : exact || clients.find(item => new URL(item.url).origin === self.location.origin && new URL(item.url).pathname !== "/login");
    if (client) {
      client.postMessage({type: "openbear:notification-open", url: target});
      await client.focus();
    } else {
      await self.clients.openWindow(target);
    }
  })());
});
