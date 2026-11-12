import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as Vue from 'vue';
import {parse, compileTemplate} from '@vue/compiler-sfc';
import {stopWorkConfirmation, stopWorkWarning, stopWorkFailure} from './stopWork.js';

const source = fs.readFileSync(new URL('./BrowserUpdate.vue', import.meta.url), 'utf8');
const {descriptor} = parse(source);
function harness({state = 'ready', supported = true, online = true, accept = true, upToDate = false} = {}) {
  const calls = [], confirmations = [], changed = [], cleanups = [];
  const props = Vue.reactive({serverId: 'test', browser: {state, chromeVersion: '130.0.0.1'}, supported, online});
  const context = {...Vue, defineProps: () => props, defineEmits: () => (...x) => changed.push(x), onBeforeUnmount: fn => cleanups.push(fn),
    ok: x => x, safeError: String,
    ElMessageBox: {confirm: async text => { confirmations.push(text); if (!accept) throw new Error('cancel'); }},
    Api: {updateServerBrowser: async (id, data) => { calls.push([id, {...data}]); return {ok: true, update: {latest: '131.0.0.2', upToDate, started: !data.check && !upToDate}}; }}};
  vm.createContext(context);
  vm.runInContext(descriptor.scriptSetup.content.replace(/^import[^\n]*\n/gm, ''), context);
  return {context, calls, confirmations, changed, props, run: code => vm.runInContext(code, context), unmount: () => cleanups.forEach(f => f())};
}

test('check does not update; update explicitly confirms closing running pages', async () => {
  const h = harness();
  await h.context.check();
  assert.deepEqual(h.calls, [['test', {check: true}]]);
  assert.equal(h.run('available.value'), '131.0.0.2');
  assert.equal(h.confirmations.length, 0);
  await h.context.update();
  assert.deepEqual(h.calls[1], ['test', {restart: true}]);
  assert.match(h.confirmations[0], /会重启浏览器，打开的页面会关闭/);
  assert.deepEqual(h.changed, [['changed']]);
});

test('cancelled restart sends no update and releases the busy state', async () => {
  const h = harness({accept: false});
  await h.context.update();
  assert.equal(h.calls.length, 0);
  assert.equal(h.run('checking.value'), false);
});

test('stopped browser updates without an implicit restart; latest reports latest', async () => {
  const h = harness({state: 'stopped'});
  await h.context.update();
  assert.deepEqual(h.calls, [['test', {restart: false}]]);
  assert.deepEqual(h.confirmations, []);
  const latest = harness({upToDate: true});
  await latest.context.check();
  assert.equal(latest.run('available.value'), '');
  assert.equal(latest.run('message.value'), '已是最新 Stable');
});

test('old and offline nodes cannot request an update; in-progress update cannot be repeated', async () => {
  for (const options of [{supported: false}, {online: false}]) {
    const h = harness(options);
    await h.context.check(); await h.context.update();
    assert.deepEqual(h.calls, []);
  }
  const h = harness();
  h.props.browser.update = {state: 'downloading'};
  await h.context.check(); await h.context.update();
  assert.deepEqual(h.calls, []);
});

test('leaving the panel while its confirmation is open cannot start the update', async () => {
  const h = harness();
  h.context.ElMessageBox.confirm = async () => { h.unmount(); };
  await h.context.update();
  assert.deepEqual(h.calls, []);
});

test('browser controls compile with version, update action and compatibility message', () => {
  assert.deepEqual(compileTemplate({source: descriptor.template.content, filename: 'BrowserUpdate.vue', id: 'browser-update'}).errors, []);
  assert.match(descriptor.template.content, /@click="check">检查更新/);
  assert.match(descriptor.template.content, /@click="update">更新到/);
  assert.match(descriptor.template.content, /browser.chromeVersion/);
  assert.match(descriptor.template.content, /需要先更新节点/);
  const detail = fs.readFileSync(new URL('./ServerDetail.vue', import.meta.url), 'utf8');
  assert.doesNotMatch(detail, /节点更新时一起更新/);
  assert.match(detail, /<BrowserUpdate/);
  assert.match(detail, /stopWorkConfirmation\(Boolean\(item.live\)\)/);
  assert.match(detail, /stopWorkConfirmation\(state === 'online'\)/);
});

test('destructive confirmations and result warnings name stopped work and offline limits', () => {
  assert.equal(stopWorkConfirmation(true), '节点上正在运行的任务和评测会被强制停止。');
  assert.match(stopWorkConfirmation(false), /节点离线，无法停止/);
  const warning = stopWorkWarning({offline: true, failed: [{id: 'task1', error: 'timeout'}], requested: [{id: 'bench1'}]});
  assert.match(warning, /节点离线/);
  assert.match(warning, /task1：停止超时/);
  assert.match(warning, /尚未确认结束/);
  assert.equal(stopWorkWarning({failed: []}), '');
});


test('an unconfirmed stop says management was retained and names the failed job', () => {
  const message = stopWorkFailure({response: {data: {error: 'server_work_stop_failed', errors: [{kind: 'job', id: 'job123', error: 'timeout'}]}}});
  assert.match(message, /未删除或卸载，管理信息已保留/);
  assert.match(message, /job123：停止超时/);
  assert.equal(stopWorkFailure({response: {data: {error: 'something_else'}}}), '');
  const detail = fs.readFileSync(new URL('./ServerDetail.vue', import.meta.url), 'utf8');
  assert.match(detail, /stopWorkFailure\(e\) \|\| safeError\(e\)/);
  assert.match(detail, /ElMessage.success\(result.note \|\|/);
});


test('browser polling does not discard live metrics, load samples or disk IO', async () => {
  const detail = fs.readFileSync(new URL('./ServerDetail.vue', import.meta.url), 'utf8');
  const metrics = detail.slice(detail.indexOf('const LIVE_KEYS ='), detail.indexOf('// Any other range:'));
  const first = {points: {t: [1000], load: [1.5], ioRead: [32], ioWrite: [64]}};
  const second = {points: {t: [1003], load: [1.6], ioRead: [40], ioWrite: [80]}};
  const recent = {value: null}, error = {value: ''}, calls = [];
  const context = {alive: true, browserRequest: 1, s: {value: {id: 'test', node: {}}}, recent, error,
    ok: x => x, safeError: String, Api: {serverMetrics: async (...args) => {
      calls.push(args);
      context.browserRequest++; // An unrelated browser request completes in the meantime.
      return calls.length === 1 ? first : second;
    }}};
  vm.createContext(context);
  vm.runInContext(metrics, context);
  await context.loadRecent();
  assert.equal(recent.value, first);
  await context.loadRecent();
  assert.deepEqual(calls, [['test', 'live', 0], ['test', 'live', 1000]]);
  assert.deepEqual(Array.from(recent.value.points.load), [1.5, 1.6]);
  assert.deepEqual(Array.from(recent.value.points.ioRead), [32, 40]);
  assert.deepEqual(Array.from(recent.value.points.ioWrite), [64, 80]);
  assert.equal(error.value, '');
});
