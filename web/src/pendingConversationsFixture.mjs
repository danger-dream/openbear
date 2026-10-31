// A conversation tree with one background conversation waiting for three answers, for
// pendingConversations.dom.test.mjs and pendingConversations.browser.test.mjs.
// handle({path, method, query}) answers HTTP; socket(socket) answers the page's sockets
// (socket.url, socket.send(text), socket.onMessage(fn)); stopWaiting() ends the wait and pushes it.
const folder = (id, parentId = '', name = id) => ({kind: 'folder', id, folderId: id, parentId, name, path: parentId ? `项目 / ${name}` : name, conversationCount: 3});
const folders = [folder('root', '', '项目'), folder('child', 'root', '后台任务')];
const row = (id, extra = {}) => ({kind: 'conversation', id, conversationUuid: id, title: id === 'background' ? '很长的后台会话名称，用来验证目录与手机待处理提示不会挤坏布局' : '当前会话',
  folderId: id === 'background' ? 'child' : 'root', parentId: id === 'background' ? 'child' : 'root', path: id === 'background' ? '项目 / 后台任务' : '项目',
  activityAtMs: 1, activityVersion: 1, activityReadVersion: 1, running: false, activityState: 'idle', ...extra});
const pending = [{interactionId: 'confirm-one', action: 'confirm', expiresAtMs: Date.now() + 3600000}, {interactionId: 'choose-two', action: 'select', expiresAtMs: Date.now() + 3600000},
  {interactionId: 'questionnaire-three', action: 'questionnaire', expiresAtMs: Date.now() + 3600000}];

export function createPendingBackend() {
  let waiting = true, eventsSocket = null, seq = 1;
  const writes = [];
  const background = () => row('background', {running: waiting, activityState: waiting ? 'waiting' : 'completed', activityPending: waiting ? pending : []});
  const status = () => ({items: waiting ? [background()] : [], activityItems: waiting ? [background()] : [], recentItems: [row('current')], folderConversationCounts: {root: 2, child: 1},
    folderRunningCounts: waiting ? {root: 1, child: 1} : {}, revision: Date.now()});
  const state = id => ({ok: true, conversationUuid: id, conversation: row(id), running: id === 'background' && waiting, messages: [], operations: [], usage: {}, frameSeq: 0});
  const forms = id => id === 'background' && waiting ? pending.map(request => ({confirmationId: request.interactionId, action: request.action, title: request.interactionId, body: '测试交互内容',
    options: ['选项一', '选项二'], questions: request.action === 'questionnaire' ? [{id: 'q1', type: 'open', question: '补充说明', required: true}] : [], createdAtMs: Date.now(), expiresAtMs: request.expiresAtMs})) : [];
  function handle({path, method, query = {}}) {
    if (method !== 'GET') writes.push(path);
    let json = {ok: true, items: []};
    if (path === '/api/conversation-tree/bootstrap') json = {ok: true, rootFolders: [folders[0]], selected: {item: row('current'), folderPath: ['root'], folderItems: [folders[0]]}, initialExpandedPaths: [['root', 'child']],
      locatedFolders: folders, running: status(), activeCount: 2, archivedCount: 0, temporaryCount: 0};
    else if (path === '/api/conversation-tree/children') json = {ok: true, items: query.parentId === 'root' ? [folders[1], row('current')] : query.parentId === 'child' ? [background()] : [], hasMore: false};
    else if (path === '/api/conversation-tree/folders') json = {ok: true, items: folders};
    else if (path === '/api/conversation-tree/status') json = {ok: true, ...status()};
    else if (path.includes('/conversation-tree/locate/')) { const id = path.split('/').at(-1); json = {ok: true, item: id === 'background' ? background() : row(id), folderPath: id === 'background' ? ['root', 'child'] : ['root'], folderItems: folders}; }
    else if (/\/conversations\/[^/]+\/state$/.test(path)) json = state(path.split('/')[3]);
    else if (path === '/api/rath/options') json = {ok: true, models: [], agents: []};
    else if (path === '/api/settings') json = {ok: true, providerCount: 1, values: {}, revision: 1};
    else if (path === '/api/auth/session') json = {ok: true, authenticated: true, mode: 'controller'};
    else if (path === '/api/system/version') json = {ok: true, version: 'test'};
    return {status: 200, json};
  }
  // Sockets of either kind: the fake network's (url, receive, onSend) or a Playwright WebSocketRoute.
  function socket(ws) {
    const url = typeof ws.url === 'function' ? ws.url() : ws.url, send = text => (ws.receive ? ws.receive(text) : ws.send(text));
    const path = new URL(url).pathname;
    if (path === '/api/events/ws') { eventsSocket = send; send(JSON.stringify({type: 'snapshot', epoch: 'test', seq, items: [], includeArchived: false, treeStatus: status()})); }
    else { const match = path.match(/\/conversations\/([^/]+)\/ws/); if (match) send(JSON.stringify({type: 'bootstrap', pendingConfirmations: forms(match[1])})); }
    const onMessage = message => { if (message === 'ping') send('pong'); };
    if (ws.onMessage) ws.onMessage(onMessage); else ws.onSend = onMessage;
  }
  function stopWaiting() {
    waiting = false;
    eventsSocket(JSON.stringify({type: 'patch', epoch: 'test', previousSeq: seq, seq: ++seq, upserts: [], removed: [], treeStatus: status()}));
  }
  return {handle, socket, stopWaiting, writes};
}
