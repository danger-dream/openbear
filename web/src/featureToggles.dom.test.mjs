// The 实例 / 主机 switches without a browser: which menus the real App lists, what the
// hidden pages say, which requests go out and where the links lead, in happy-dom.
// Layout of the same pages (no sideways scroll on desktop, phone, dark) stays in
// featureToggles.browser.test.mjs.
import {window} from './domTestEnv.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import {openPage} from './testing/appHarness.mjs';
import {sleep} from './testing/domPage.mjs';
import {featureApi, createFeatureState} from './featureTogglesFixture.mjs';

const {resetFeatureTogglesForTest} = await import('./featureToggles.js');
const App = (await import('./App.vue')).default;

const state = createFeatureState();
// One "page load": a fresh App at `path`, as the browser test's open() does with page.goto.
async function open(t, {width, dark = false, path}) {
  const h = await openPage(t, App, {url: path, width, height: 844, dark, handler: request => featureApi(state, request),
    storage: {'openbear.notifications.prompt.v1': 'dismissed', 'openbear.theme.v1': dark ? 'dark' : 'light'},
    onSocket: socket => { if (socket.url.includes('/api/events/ws')) socket.receive(JSON.stringify({type: 'snapshot', epoch: 'test', seq: 1, items: [], includeArchived: false})); },
    before: resetFeatureTogglesForTest});
  return h;
}
// Labels the menus actually show for the two optional pages.
async function menuLabels({page, phone}) {
  if (!phone) {
    await page.locator('.sidebar-desktop-nav').waitFor();
    const tiles = await page.locator('.sidebar-desktop-nav .sidebar-resource-label').allTextContents();
    if (await page.getByRole('button', {name: '更多入口'}).count()) {
      await page.getByRole('button', {name: '更多入口'}).click();
      tiles.push(...await page.locator('.sidebar-more-go span').allTextContents());
      await page.keyboard.press('Escape');
    }
    return tiles.map(s => s.trim());
  }
  await page.getByRole('button', {name: '打开导航', exact: true}).filter({visible: true}).first().click();
  await page.getByRole('button', {name: '资源与设置', exact: true}).click();
  const grid = page.locator('.sidebar-resources-grid').filter({visible: true});
  await grid.waitFor();
  const labels = (await grid.locator('.sidebar-resource-label').allTextContents()).map(s => s.trim());
  await page.keyboard.press('Escape');
  return labels;
}
// Desktop tiles use short labels, the mobile grid and "更多" panel full ones.
const KEYS = {'实例': 'instances', '实例管理': 'instances', '主机': 'servers', '服务器': 'servers'};
const optional = labels => [...new Set(labels.map(l => KEYS[l]).filter(Boolean))].sort();
const called = (h, path) => h.calls.some(c => c.path === path);

for (const width of [1440, 390]) {
  test(`${width}px new controller, both off: menus hide them, settings turns one on at once`, async t => {
    Object.assign(state, {controller: 'new', values: {'features.instances': false, 'features.servers': false}, settingsDelayMs: 0, settingsFail: false, patches: []});
    for (const dark of [false, true]) {
      const h = await open(t, {width, dark, path: '/settings?section=system-settings'});
      const row = h.page.locator('.settings-row', {hasText: '启用主机'});
      await row.waitFor();
      assert.match(await row.textContent(), /token/);
      assert.equal((await row.locator('.setting-state-text').textContent()).trim(), '关闭');
      assert.deepEqual(optional(await menuLabels(h)), []);
      if (dark) {
        await row.locator('.mac-toggle').click();
        await h.page.waitForFunction(() => [...document.querySelectorAll('.settings-row')].find(r => r.textContent.includes('启用主机'))?.querySelector('.setting-state-text')?.textContent.trim() === '开启');
        assert.deepEqual(state.patches, [{key: 'features.servers', value: true}]);
        assert.deepEqual(optional(await menuLabels(h)), ['servers']);
      }
      await h.unmount();
    }
  });

  test(`${width}px direct link to a hidden page explains and turns it on`, async t => {
    Object.assign(state, {controller: 'new', values: {'features.instances': false, 'features.servers': false}, settingsDelayMs: 0, settingsFail: false, patches: []});
    for (const dark of [false, true]) {
      const h = await open(t, {width, dark, path: '/servers'});
      await h.page.getByText('主机功能未开启', {exact: true}).waitFor();
      assert.ok(!called(h, '/api/servers'), 'the hidden page itself is not loaded');
      await h.unmount();
    }
    const h = await open(t, {width, path: '/instances'});
    await h.page.getByText('实例功能未开启', {exact: true}).waitFor();
    assert.equal(new URL(h.page.url()).pathname, '/instances');
    await h.page.getByRole('button', {name: '开启实例'}).click();
    await h.page.getByText('还没有启用访问网关', {exact: true}).waitFor();
    assert.deepEqual(state.patches, [{key: 'features.instances', value: true}]);
    assert.deepEqual(optional(await menuLabels(h)), ['instances']);
    await h.unmount();
    // The settings entry lands on that switch.
    state.values['features.instances'] = false;
    const g = await open(t, {width, path: '/instances'});
    await g.page.getByRole('button', {name: '在系统设置中管理'}).click();
    await g.page.locator('.settings-row', {hasText: '启用实例'}).waitFor();
    assert.equal(new URL(g.page.url()).searchParams.get('setting'), 'features.instances');
    assert.equal(await g.page.locator('.settings-row', {hasText: '启用主机'}).count(), 0);
    await g.unmount();
  });

  test(`${width}px older controller without the setting: everything stays on`, async t => {
    Object.assign(state, {controller: 'old', values: {}, settingsDelayMs: 0, settingsFail: false, patches: []});
    const h = await open(t, {width, path: '/settings?section=system-settings'});
    await h.page.locator('.settings-row', {hasText: '显示本轮统计'}).waitFor();
    assert.deepEqual(optional(await menuLabels(h)), ['instances', 'servers']);
    await h.unmount();
    const g = await open(t, {width, path: '/instances'});
    await g.page.getByText('还没有启用访问网关', {exact: true}).waitFor();
    assert.equal(await g.page.getByText('实例功能未开启').count(), 0);
    await g.unmount();
  });

  test(`${width}px unreadable settings keep both reachable; a slow answer never flashes the hidden page`, async t => {
    Object.assign(state, {controller: 'new', values: {'features.instances': false}, settingsDelayMs: 0, settingsFail: true, patches: []});
    const h = await open(t, {width, path: '/settings?section=sessions'});
    // Was waitForTimeout(300): wait for the failed settings read the menus fall back on.
    const answered = state.settingsAnswers;
    await h.page.waitForFunction(() => state.settingsAnswers > answered);
    assert.deepEqual(optional(await menuLabels(h)), ['instances', 'servers']);
    await h.unmount();
    Object.assign(state, {settingsFail: false, settingsDelayMs: 600});
    const g = await open(t, {width, path: '/instances'});
    // While the settings answer is outstanding: neither the page's data nor the hidden notice.
    await sleep(300);
    assert.equal(state.settingsAnswers, answered + 1, 'the settings answer is still outstanding');
    assert.ok(!called(g, '/api/instances'));
    assert.equal(await g.page.getByText('实例功能未开启').count(), 0);
    await g.page.getByText('实例功能未开启', {exact: true}).waitFor();
    assert.ok(!called(g, '/api/instances'));
    state.settingsDelayMs = 0;
    await g.unmount();
  });
}
