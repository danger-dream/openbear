import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import * as Vue from 'vue';
import {compile} from '@vue/compiler-dom';
import {renderToString} from '@vue/server-renderer';
import {activityLabel, isActivityWaiting} from '../conversationActivity.js';

const source = fs.readFileSync(new URL('./ConversationTree.vue', import.meta.url), 'utf8');
const buttons = [...source.matchAll(/<button class="tree-node-main(?: conversation)?"[\s\S]*?<\/button>/g)].map(match => match[0]);
assert.equal(buttons.length, 2);
const text = html => html.replace(/<[^>]*>/g, '').trim();
const script = source.match(/<script setup>([\s\S]*?)<\/script>/)[1];
const originSource = script.match(/function conversationOrigin\(row\) \{[\s\S]*?\n\}/)[0];
const realConversationOrigin = new Function('Clock', 'Connection', 'ChatLineRound', 'SetUp', 'Tools', `${originSource}; return conversationOrigin;`)(
  'clock-icon', 'connection-icon', 'chat-icon', 'deployment-icon', 'maintenance-icon');
async function rowHtml(row, {origins = false} = {}) {
  const render = new Function('Vue', compile(buttons[row.kind === 'conversation' ? 1 : 0], {mode: 'function', prefixIdentifiers: true}).code)(Vue);
  const icon = {render() { return Vue.h('i', this.$slots.default?.()); }};
  const animated = {props: ['text'], render() {return Vue.h('span', this.text);}};
  return renderToString(Vue.createSSRApp({render, components: {ElIcon: icon, InfoFilled: icon, StarFilled: icon, AnimatedConversationTitle: animated},
    setup: () => ({row, activeConversationRow: row.conversationUuid === 'active' ? row : null, selectedFolderId: 'project',
      running: row => Boolean(row.running || row.status === 'running'), rowLoading: () => false, isTitleGenerating: () => false, isExpanded: () => true,
      liveConversationTitle: row => row.title || '新会话', conversationOrigin: origins ? realConversationOrigin : () => ({icon: 'chat-icon'}),
      activityLabel, isActivityWaiting, folderWaitingCount:row=>row.waitingCount || 0, collapsedFolderActivity:row=>row.activity || null, activateRow() {}, openMenu() {}, locateAndOpen() {},
      ChatLineRound: 'chat-icon', Loading: 'loading-icon', Box: 'box-icon', FolderOpened: 'folder-icon', Folder: 'folder-icon'}),
  }));
}

for (const state of ['running']) test(`ordinary ${state} row keeps blue ring and just one textless activity dot`, async () => {
  const html = await rowHtml({kind: 'conversation', conversationUuid: 'active', title: 'Current task', running: true,
    activityState: state, currentStatus: state === 'waiting' ? '待填写问卷' : '运行中', activityUnread: true});
  assert.equal(text(html), 'Current task');
  assert.match(html, /node-icon is-working/);
  assert.equal((html.match(/class="running-leaf"/g) || []).length, 1);
  assert.doesNotMatch(html, /conversation-unread-dot|is-waiting/);
  assert.match(html, /title="[^\"]*未读"/);
  assert.match(html, /class="running-leaf"[^>]*><\/span>/);
});

test('waiting row uses an explicit warning label instead of a running ring or unread dot', async () => {
  const html = await rowHtml({kind:'conversation',title:'Waiting task',running:true,activityUnread:true,activityPending:[{action:'confirm'}]});
  assert.equal(text(html), 'Waiting task待确认');
  assert.match(html, /tree-waiting-state/);
  assert.doesNotMatch(html, /running-leaf|is-working|conversation-unread-dot/);
  const folder = await rowHtml({kind:'folder',folderId:'project',name:'Project',conversationCount:10,waitingCount:2});
  assert.match(folder, /aria-label="2 个会话待处理"/);
});

test('collapsed folder adds running and unread dots after its waiting label', async () => {
  const html = await rowHtml({kind:'folder',folderId:'project',name:'Project',conversationCount:10,waitingCount:1,activity:{running:2,unread:1}});
  assert.match(html, /待处理 1/);
  assert.match(html, /class="running-leaf"[^>]*aria-label="2 个会话运行中"/);
  assert.match(html, /class="conversation-unread-dot"[^>]*aria-label="1 个会话有未读结果"/);
  const unreadOnly = await rowHtml({kind:'folder',folderId:'project',name:'Project',conversationCount:10,activity:{running:0,unread:3}});
  assert.doesNotMatch(unreadOnly, /running-leaf/);
  assert.match(unreadOnly, /aria-label="3 个会话有未读结果"/);
});

test('completed unread row retains its unread dot without the running animation or label', async () => {
  const html = await rowHtml({kind: 'conversation', title: 'Done task', activityState: 'completed', activityUnread: true});
  assert.equal(text(html), 'Done task');
  assert.match(html, /conversation-unread-dot/);
  assert.doesNotMatch(html, /running-leaf|is-working/);
});

test('system-created conversations show their feature icon and label; user conversations keep the chat icon', async () => {
  for (const [origin, icon, label] of [['cron', 'clock-icon', '定时任务创建'], ['webhook', 'connection-icon', '触发器创建'], ['deployment', 'deployment-icon', '实例部署会话'], ['maintenance', 'maintenance-icon', '实例维护会话']]) {
    const html = await rowHtml({kind: 'conversation', conversationUuid: origin, title: 'Run', origin}, {origins: true});
    assert.match(html, new RegExp(`<${icon}`));
    assert.match(html, new RegExp(`<i class="node-icon"[^>]* title="${label}" aria-label="${label}"`));
    assert.equal(text(html), 'Run');
  }
  for (const origin of ['user', undefined]) {
    const html = await rowHtml({kind: 'conversation', conversationUuid: 'mine', title: 'Mine', origin}, {origins: true});
    assert.match(html, /<chat-icon/);
    assert.doesNotMatch(html, /创建/);
  }
  // While running, the origin icon keeps the same working ring.
  const running = await rowHtml({kind: 'conversation', conversationUuid: 'r', title: 'Run', origin: 'cron', running: true}, {origins: true});
  assert.match(running, /node-icon is-working/);
  assert.match(running, /<clock-icon/);
});

test('configured folder keeps count and pin without running or property badges', async () => {
  const html = await rowHtml({kind: 'folder', folderId: 'project', name: 'Project', runningDescendantCount: 3,
    conversationCount: 10, pinned: true, hasLocalWorkspace: true, hasLocalPrompt: true});
  assert.doesNotMatch(html, /running-count|运行中/);
  assert.match(html, /node-count[^>]*>10<\/span>/);
  assert.match(html, /node-star/);
  assert.doesNotMatch(html, /property-dot/);
});
