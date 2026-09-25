import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as Vue from 'vue';
import {parse, compileTemplate} from '@vue/compiler-sfc';

const source = fs.readFileSync(new URL('./SettingsView.vue', import.meta.url), 'utf8');
function runtime(probe) {
  const script = parse(source).descriptor.scriptSetup.content.replace(/^import .*;\n/gm, '');
  const calls = [];
  const ctx = vm.createContext({...Vue, onMounted() {},
    Api: {testBrowserConnection: async endpoint => {calls.push(endpoint); return probe(endpoint);},
      updateSetting() {assert.fail('a connection test must not save or enable');}},
    apiError: error => error.message,
  });
  vm.runInContext(script, ctx);
  return {calls, run: code => vm.runInContext(code, ctx)};
}

test('test connection checks the unsaved address without saving or enabling', async () => {
  const r = runtime(async () => ({ok: true, product: 'Chromium/test', elapsedMs: 12}));
  r.run('draft["browser.mainEndpoint"] = " http://draft.invalid "; draft["browser.enabled"] = false');
  await r.run('testBrowserConnection()');
  assert.deepEqual(r.calls, ['http://draft.invalid']);
  assert.equal(r.run('draft["browser.enabled"]'), false);
  assert.equal(r.run('browserProbeState.value.ok'), true);
  assert.match(r.run('browserProbeState.value.text'), /Chromium\/test.*12 ms.*不保存或启用/);
  assert.equal(r.run('testingBrowser.value'), false);
});

test('empty input, duplicate clicks and late results do not issue extra probes or validate another address', async () => {
  let finish;
  const r = runtime(() => new Promise(resolve => {finish = resolve;}));
  await r.run('testBrowserConnection()');
  assert.deepEqual(r.calls, []);
  r.run('draft["browser.mainEndpoint"] = "http://first.invalid"');
  const pending = r.run('testBrowserConnection()');
  assert.equal(r.run('testingBrowser.value'), true);
  await r.run('testBrowserConnection()');
  assert.equal(r.calls.length, 1);
  r.run('draft["browser.mainEndpoint"] = "http://second.invalid"');
  finish({ok: true, product: 'Test/1'});
  await pending;
  assert.equal(r.run('browserProbeState.value'), null);
  assert.equal(r.run('draft["browser.mainEndpoint"]'), 'http://second.invalid');
});

test('protocol and transport failures display the error and release the busy state', async () => {
  for (const probe of [async () => ({ok: false, error: '连接失败'}), async () => {throw new Error('网络错误');}]) {
    const r = runtime(probe);
    r.run('draft["browser.mainEndpoint"] = "http://failed.invalid"');
    await r.run('testBrowserConnection()');
    assert.equal(r.run('browserProbeState.value.ok'), false);
    assert.match(r.run('browserProbeState.value.text'), /连接失败|网络错误/);
    assert.equal(r.run('testingBrowser.value'), false);
  }
});

test('settings template compiles and the test button preserves unsaved input on mouse-down', () => {
  const descriptor = parse(source).descriptor;
  const compiled = compileTemplate({source: descriptor.template.content, filename: 'SettingsView.vue', id: 'browser-settings'});
  assert.deepEqual(compiled.errors, []);
  const markup = source.match(/<div v-if="spec.path === 'browser.mainEndpoint'"[\s\S]*?<\/div>/)[0];
  assert.match(markup, /@mousedown.prevent/);
  assert.match(markup, /@click="testBrowserConnection"/);
  assert.match(markup, /role="status"/);
  assert.match(markup, /:disabled="testingBrowser \|\| saving\[spec.path\] \|\| !String/);
});
