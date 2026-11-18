import test from 'node:test';
import assert from 'node:assert/strict';
import {setup, showTab} from './testing/serversDom.mjs';
import {network} from '../testing/fakeNetwork.mjs';
import {waitUntil} from '../testing/domPage.mjs';
import {browserRestartFeedback} from './browserStatus.js';

test('C5 stale SSH check is not announced as a current success or failure', async t => {
  const h = await setup(t);
  const backend = network.handler;
  let status = 'ok';
  network.handler = async request => request.path === '/api/instance-servers/srv-hotel/check'
    ? {status: 200, json: {ok: true, stale: true, item: {status}}}
    : backend(request);
  await h.page.locator('.pb-card').filter({hasText: 'Hotel-HK'}).click();
  const detail = h.page.locator('.sv-detail:visible');
  const card = detail.locator('[data-part="ssh"]:visible');
  for (const returned of ['ok', 'unreachable']) {
    status = returned;
    const before = h.messages.length;
    await card.getByRole('button', {name: '测试', exact: true}).click();
    await waitUntil(() => h.messages.slice(before).some(m => m.type === 'info' && m.text === '连接配置已更新，已忽略旧检查结果'));
    assert.equal(h.messages.slice(before).some(m => ['连接正常', '连接失败'].includes(m.text)), false);
  }
  await h.close();
});

for (const width of [390, 1440]) {
  test(`BROWSER-07 restart feedback follows actual status (${width}px)`, async t => {
    const h = await setup(t, {width, tweak: items => {
      const item = items.find(i => i.id === 'srv-delta');
      item.node.caps.browser = item.live.caps.browser = true;
      item.browserState = {enabled: true, state: 'ready', source: 'download', product: 'Chrome/130.0.0.1'};
    }});
    const backend = network.handler;
    let resultState = 'failed';
    network.handler = async request => {
      if (request.path.endsWith('/browser/restart')) {
        const browser = {enabled: true, state: resultState, error: resultState === 'failed' ? 'endpoint refused connection' : ''};
        h.items.find(i => i.id === 'srv-delta').browserState = browser;
        return {status: 200, json: {ok: true, browser}};
      }
      return backend(request);
    };
    await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
    const detail = h.page.locator('.sv-detail:visible');
    await showTab(detail, '工具');
    const card = detail.locator('[data-part="browser"]');
    await card.getByText('运行中', {exact: true}).waitFor();
    for (const state of ['failed', 'starting', 'installing', 'ready']) {
      resultState = state;
      const before = h.messages.length;
      await card.getByRole('button', {name: '重启', exact: true}).click();
      await h.page.locator('.el-message-box').getByRole('button', {name: '重启', exact: true}).click();
      if (state === 'failed') {
        await detail.getByText('endpoint refused connection', {exact: false}).first().waitFor();
        assert.equal(h.messages.slice(before).some(m => m.type === 'success'), false);
      } else {
        const expected = browserRestartFeedback({state});
        await waitUntil(() => h.messages.slice(before).some(m => m.type === expected.type && m.text === expected.message));
        assert.equal(h.messages.slice(before).some(m => m.type === 'success'), state === 'ready');
      }
    }
    await h.close();
  });
}

test('BROWSER-05 installed version is separate from a surviving window; BROWSER-08 ready status is refreshed', async t => {
  const h = await setup(t, {tweak: items => {
    const item = items.find(i => i.id === 'srv-delta');
    item.node.caps.browser = item.live.caps.browser = true;
    item.browserState = {enabled: true, state: 'ready', source: 'unknown', runningSource: 'unknown', runningVersion: 'Chrome/130.0.0.1', runningVerified: false,
      chromeVersion: '131.0.0.2', restartNeeded: true, update: {state: 'done', version: '131.0.0.2', restartNeeded: true}};
  }});
  await h.page.locator('.pb-card').filter({hasText: 'Delta-JP'}).click();
  const detail = h.page.locator('.sv-detail:visible');
  await showTab(detail, '工具');
  const card = detail.locator('[data-part="browser"]');
  await card.getByText('运行中', {exact: true}).waitFor();
  const text = await card.innerText();
  assert.match(text, /已安装版本：131\.0\.0\.2/);
  assert.match(text, /实际运行：Chrome\/130\.0\.0\.1/);
  assert.match(text, /运行来源未核实/);
  assert.match(text, /现有页面未关闭/);
  h.items.find(i => i.id === 'srv-delta').browserState = {enabled: true, state: 'stopped', error: 'connection to the browser was lost'};
  await card.getByText('未启动，第一次使用时启动', {exact: true}).waitFor();
  assert.equal(h.calls.some(c => c.path.endsWith('/browser/restart')), false);
  await h.close();
});
