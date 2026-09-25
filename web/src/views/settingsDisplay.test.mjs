import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as Vue from 'vue';
import {settingDisplayValue, settingStorageValue, settingRangeLabel} from './settingsDisplay.js';

const MB = 1024 * 1024;
const body = {path:'browser.maxBodyBytes', kind:'int', unit:'MB', displayScale:MB, min:1024, max:16 * MB};

test('size and idle-time editors convert display units without changing stored values', () => {
  assert.equal(settingDisplayValue(body, 2 * MB), 2);
  assert.equal(settingStorageValue(body, '1.5'), 1572864);
  assert.equal(settingRangeLabel(body), '1 KB ～ 16 MB');
  const idle = {kind:'int', unit:'分钟', displayScale:60, min:60, max:86400};
  assert.equal(settingDisplayValue(idle, 1800), 30);
  assert.equal(settingStorageValue(idle, '45'), 2700);
  for (const value of ['', 'not-a-number', Infinity, '17', '-1']) assert.throws(() => settingStorageValue(body, value));
  assert.throws(() => settingStorageValue(body, '17'), /16 MB/);
  const ordinary = {kind:'float', unit:'秒', min:1, max:300};
  assert.equal(settingStorageValue(ordinary, '20'), '20');
  assert.equal(settingRangeLabel(ordinary), '1 ～ 300');
});

function runtime() {
  const source = fs.readFileSync(new URL('./SettingsView.vue', import.meta.url), 'utf8');
  const script = source.split('<script setup>')[1].split('</script>')[0].replace(/^import .*;\n/gm, '');
  const calls = [], errors = [], success = [];
  let stored = 2 * MB;
  const ctx = vm.createContext({...Vue, onMounted:()=>{},
    settingDisplayValue, settingStorageValue, settingRangeLabel,
    Api:{updateSetting:async (path, value)=>{calls.push({path,value});stored=value;return {ok:true};},
      settings:async()=>({ok:true, values:{[body.path]:stored}})},
    ElMessage:{error:msg=>errors.push(msg),success:msg=>success.push(msg),info:()=>{}},apiError:err=>err.message,
  });
  vm.runInContext(script, ctx);
  const run = code => vm.runInContext(code, ctx);
  ctx.testSpec = {...body, title:'网页请求内容的读取上限'};
  run('specs.value = {[testSpec.path]:testSpec}; values.value = {[testSpec.path]:2097152}; hydrateDraft();');
  return {run,calls,errors,success};
}

test('actual settings component displays MB, saves bytes, reloads MB and resets invalid input', async () => {
  const r = runtime();
  assert.equal(r.run('displayedValue(testSpec)'), '2MB');
  assert.equal(r.run('draft[testSpec.path]'), 2);
  assert.equal(r.run('isDirty(testSpec)'), false);
  r.run('draft[testSpec.path] = "1.5"');
  assert.equal(await r.run('save(testSpec)'), true);
  assert.deepEqual(r.calls, [{path:body.path,value:1572864}]);
  assert.equal(r.run('draft[testSpec.path]'), 1.5);
  assert.equal(r.run('isDirty(testSpec)'), false);
  r.run('draft[testSpec.path] = "17"');
  assert.equal(await r.run('save(testSpec)'), false);
  assert.equal(r.calls.length, 1);
  assert.match(r.errors[0], /16 MB/);
  assert.equal(r.run('draft[testSpec.path]'), 1.5);
  r.run('draft[testSpec.path] = "3"; reset(testSpec);');
  assert.equal(r.run('draft[testSpec.path]'), 1.5);
});
