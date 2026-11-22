// AssistantDock: picking a cross-page task whose sub-object mounts asynchronously does not
// transiently yank the panel to the intermediate parent page's conversation.
import '../domTestEnv.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import { defineComponent, h, nextTick, ref } from 'vue';
import { mountApp } from '../testing/mountApp.mjs';
import { Api } from '../api.js';
import { applyAssistantSettings, resetAssistantSettingsForTest } from './assistantSettings.js';
import { setPageInfo, usePageSubject } from './pageRegistry.js';

const AssistantDock = (await import('./AssistantDock.vue')).default;

test('picking a sub-object task keeps its conversation across the intermediate parent page', async t => {
  resetAssistantSettingsForTest();
  t.after(resetAssistantSettingsForTest);
  applyAssistantSettings({'assistant.enabled': true, 'assistant.contextChars': 4000});

  const activePage = ref('settings');
  const detailMounted = ref(false);
  setPageInfo(() => ({key: activePage.value, label: activePage.value === 'servers' ? '服务器' : '系统设置'}));

  const taskItem = {
    conversationUuid: 'conv-srv-1',
    title: '排查服务器',
    assistantScope: {key: 'servers|server:srv-1', label: '服务器 · Delta', path: '/servers', open: {kind: 'server', id: 'srv-1'}},
    running: false,
    activityUnread: false,
  };
  let scopeCalls = [];
  t.mock.method(Api, 'assistantConversations', async (params = {}) => {
    scopeCalls.push(params.scope || '');
    if (params.scope === 'servers') return {ok: true, items: [], latest: 'conv-servers-parent'};
    if (params.scope === 'settings') return {ok: true, items: [], latest: 'conv-settings'};
    return {ok: true, items: [taskItem], latest: ''};
  });
  t.mock.method(Api, 'conversationState', async () => ({ok: true, messages: [], operations: [], timeline: [], runConfig: {}}));
  t.mock.method(Api, 'conversationDefaults', async () => ({ok: true, defaults: {}}));
  t.mock.method(Api, 'rathOptions', async () => ({ok: true, models: []}));
  t.mock.method(Api, 'folderExtensions', async () => ({ok: true, extensions: [], default: {enabled: []}}));
  t.mock.method(Api, 'conversationExtensions', async () => ({ok: true, extensions: [], state: {enabled: []}}));

  const SubjectProbe = defineComponent({
    setup() {
      usePageSubject(() => detailMounted.value ? {kind: 'server', id: 'srv-1', label: 'Delta', main: true} : null);
      return () => h('div', 'probe');
    },
  });

  const {page} = mountApp(t, {
    render: () => h('div', [
      h(SubjectProbe),
      h(AssistantDock, {page: activePage.value}),
    ]),
  });

  await page.locator('.assistant-ball').click();
  await nextTick();
  await new Promise(r => setTimeout(r, 20));
  scopeCalls = [];

  let openPageFired = '';
  const onOpenPage = e => { openPageFired = e.detail?.path || ''; };
  window.addEventListener('openbear:open-page', onOpenPage);
  t.after(() => window.removeEventListener('openbear:open-page', onOpenPage));

  await page.locator('.assistant-tasks-button').click();
  await nextTick();
  await new Promise(r => setTimeout(r, 20));
  await page.getByText('排查服务器').first().click();
  await nextTick();
  assert.equal(openPageFired, '/servers');

  // Switch page to 'servers' BEFORE ServerDetail mounts (intermediate parent scope).
  activePage.value = 'servers';
  await nextTick();
  await new Promise(r => setTimeout(r, 20));

  assert.ok(!scopeCalls.includes('servers'), `should not follow intermediate servers scope, got calls: ${scopeCalls}`);
  assert.equal(await page.locator('.assistant-title span').textContent(), '排查服务器');

  detailMounted.value = true;
  await nextTick();
  assert.equal(await page.locator('.assistant-title span').textContent(), '排查服务器');
});
