import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import * as Vue from 'vue';
import {compile} from '@vue/compiler-dom';
import {renderToString} from '@vue/server-renderer';
import {activityLabel} from '../conversationActivity.js';

const source = fs.readFileSync(new URL('./ConversationTree.vue', import.meta.url), 'utf8');
const buttons = [...source.matchAll(/<button class="tree-node-main(?: conversation)?"[\s\S]*?<\/button>/g)].map(match => match[0]);
assert.equal(buttons.length, 2);
const text = html => html.replace(/<[^>]*>/g, '').trim();
async function rowHtml(row) {
  const render = new Function('Vue', compile(buttons[row.kind === 'conversation' ? 1 : 0], {mode: 'function', prefixIdentifiers: true}).code)(Vue);
  const icon = {render() { return Vue.h('i', this.$slots.default?.()); }};
  const animated = {props: ['text'], render() {return Vue.h('span', this.text);}};
  return renderToString(Vue.createSSRApp({render, components: {ElIcon: icon, InfoFilled: icon, StarFilled: icon, AnimatedConversationTitle: animated},
    setup: () => ({row, activeConversationUuid: 'active', selectedFolderId: 'project',
      running: row => Boolean(row.running || row.status === 'running'), rowLoading: () => false, isTitleGenerating: () => false, isExpanded: () => true,
      liveConversationTitle: row => row.title || '新会话',
      activityLabel, activateRow() {}, openMenu() {}, locateAndOpen() {},
      ChatLineRound: 'chat-icon', Loading: 'loading-icon', Box: 'box-icon', FolderOpened: 'folder-icon', Folder: 'folder-icon'}),
  }));
}

for (const state of ['running', 'waiting']) test(`ordinary ${state} row keeps blue ring and just one textless activity dot`, async () => {
  const html = await rowHtml({kind: 'conversation', conversationUuid: 'active', title: 'Current task', running: true,
    activityState: state, currentStatus: state === 'waiting' ? '待填写问卷' : '运行中', activityUnread: true});
  assert.equal(text(html), 'Current task');
  assert.match(html, /node-icon is-working/);
  assert.equal((html.match(/class="running-leaf"/g) || []).length, 1);
  assert.doesNotMatch(html, /conversation-unread-dot|is-waiting/);
  assert.match(html, /title="[^\"]*未读"/);
  assert.match(html, /class="running-leaf"[^>]*><\/span>/);
});

test('completed unread row retains its unread dot without the running animation or label', async () => {
  const html = await rowHtml({kind: 'conversation', title: 'Done task', activityState: 'completed', activityUnread: true});
  assert.equal(text(html), 'Done task');
  assert.match(html, /conversation-unread-dot/);
  assert.doesNotMatch(html, /running-leaf|is-working/);
});

test('configured folder keeps count and pin without running or property badges', async () => {
  const html = await rowHtml({kind: 'folder', folderId: 'project', name: 'Project', runningDescendantCount: 3,
    conversationCount: 10, pinned: true, hasLocalWorkspace: true, hasLocalPrompt: true});
  assert.doesNotMatch(html, /running-count|运行中/);
  assert.match(html, /node-count[^>]*>10<\/span>/);
  assert.match(html, /node-star/);
  assert.doesNotMatch(html, /property-dot/);
});

test('running dot is compact, blue in both themes and breathes without changing geometry', () => {
  const dot = source.match(/^\.running-leaf \{([^}]+)\}/m)[1];
  assert.match(dot, /width:6px; height:6px; flex:0 0 6px/);
  assert.match(dot, /background:var\(--ob-blue\)/);
  assert.match(dot, /animation:tree-pulse 1\.8s ease-in-out infinite/);
  assert.doesNotMatch(source, /html\.dark[^{}]*\.running-leaf/);
  assert.match(source, /@keyframes tree-pulse \{ 50% \{ opacity:\.35; transform:scale\(\.8\);/);
  assert.match(source, /prefers-reduced-motion: reduce\) \{ \.is-spinning,\.running-leaf \{ animation:none/);
  assert.doesNotMatch(source, /\.running-count|\.running-leaf i|\.running-leaf\.is-waiting/);
  assert.match(source, /\.node-icon\.is-working::before[^\n]*animation:tree-work-border-spin/);
});
