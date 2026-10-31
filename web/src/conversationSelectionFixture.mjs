// One folder with a hand-made conversation and 60 automatic ones over two pages, for
// conversationSelection.dom.test.mjs and conversationSelection.browser.test.mjs.
// handle({path, method, query, body}) answers HTTP (batch deletes really delete); socket(ws)
// answers the page's sockets (fake network socket or Playwright WebSocketRoute).
const folder = {kind: 'folder', id: 'discord', folderId: 'discord', parentId: '', name: 'Discord监控', path: 'Discord监控', conversationCount: 61};
const event = index => ({kind: 'conversation', id: `event-${index}`, conversationUuid: `event-${index}`, title: `10-25 17:${String(index).padStart(2, '0')} · 外部事件 · ${index.toString(16).padStart(6, '0')}`,
  folderId: 'discord', parentId: 'discord', path: 'Discord监控', origin: 'webhook', lastInteractionAtMs: 0, activityVersion: 2, activityReadVersion: 1, activityUnread: true, activityState: 'completed', running: false});
const manual = {kind: 'conversation', id: 'manual', conversationUuid: 'manual', title: '控制 Discord 网页版', folderId: 'discord', parentId: 'discord', path: 'Discord监控', origin: 'user',
  lastInteractionAtMs: 1000, activityVersion: 1, activityReadVersion: 1, activityUnread: false, activityState: 'completed', running: false};

export function createSelectionBackend() {
  const deleted = new Set(), batches = [];
  const conversations = () => [manual, ...Array.from({length: 60}, (_, index) => event(index))].filter(row => !deleted.has(row.conversationUuid));
  const status = () => ({items: [], activityItems: conversations().filter(row => row.activityUnread), recentItems: [manual], folderConversationCounts: {discord: conversations().length}, folderRunningCounts: {}, revision: Date.now()});
  function handle({path, query = {}, body}) {
    let json = {ok: true, items: []};
    if (path === '/api/conversation-tree/bootstrap') json = {ok: true, rootFolders: [{...folder, conversationCount: conversations().length}], selected: {item: manual, folderPath: ['discord'], folderItems: [folder]},
      initialExpandedPaths: [['discord']], locatedFolders: [folder], running: status(), activeCount: conversations().length, archivedCount: 0, temporaryCount: 0};
    else if (path === '/api/conversation-tree/children') {
      const rows = query.parentId === 'discord' ? conversations() : [];
      const offset = Number(query.cursor || 0), limit = Number(query.limit || 50);
      json = {ok: true, items: rows.slice(offset, offset + limit), hasMore: offset + limit < rows.length, nextCursor: offset + limit < rows.length ? String(offset + limit) : ''};
    }
    else if (path === '/api/conversation-tree/folders') json = {ok: true, items: [folder]};
    else if (path === '/api/conversation-tree/status') json = {ok: true, ...status()};
    else if (path.includes('/conversation-tree/locate/')) json = {ok: true, item: manual, folderPath: ['discord'], folderItems: [folder]};
    else if (/\/conversations\/[^/]+\/state$/.test(path)) json = {ok: true, conversationUuid: 'manual', conversation: manual, running: false, messages: [], operations: [], usage: {}, frameSeq: 0};
    else if (path === '/api/conversations/batch') {
      batches.push(body);
      for (const id of body.conversationIds) deleted.add(id);
      json = {ok: true, action: body.action, results: body.conversationIds.map(id => ({conversationUuid: id, ok: true})), succeeded: body.conversationIds.length, failed: 0};
    }
    else if (path === '/api/auth/session') json = {ok: true, authenticated: true, mode: 'controller'};
    else if (path === '/api/system/version') json = {ok: true, version: 'test'};
    return {status: 200, json};
  }
  function socket(ws) {
    const url = typeof ws.url === 'function' ? ws.url() : ws.url, send = text => (ws.receive ? ws.receive(text) : ws.send(text));
    if (new URL(url).pathname === '/api/events/ws') send(JSON.stringify({type: 'snapshot', epoch: 'test', seq: 1, items: [], includeArchived: false, treeStatus: status()}));
    const onMessage = message => { if (message === 'ping') send('pong'); };
    if (ws.onMessage) ws.onMessage(onMessage); else ws.onSend = onMessage;
  }
  return {handle, socket, batches};
}
