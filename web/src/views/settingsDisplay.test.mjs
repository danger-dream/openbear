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
  assert.equal(settingRangeLabel(body), '约 0.000976563 MB ～ 16 MB');
  const idle = {kind:'int', unit:'分钟', displayScale:60, min:60, max:86400};
  assert.equal(settingDisplayValue(idle, 1800), 30);
  assert.equal(settingStorageValue(idle, '45'), 2700);
  for (const value of ['', 'not-a-number', Infinity, '17', '-1']) assert.throws(() => settingStorageValue(body, value));
  assert.throws(() => settingStorageValue(body, '17'), /16 MB/);
  const ordinary = {kind:'float', unit:'秒', min:1, max:300};
  assert.equal(settingStorageValue(ordinary, '20'), '20');
  assert.equal(settingRangeLabel(ordinary), '1 秒 ～ 300 秒');
});

test('one-sided limits describe minimum or maximum instead of a dash', () => {
  assert.equal(settingRangeLabel({min:1,max:null,unit:'天'}), '至少 1 天');
  assert.equal(settingRangeLabel({min:null,max:60,unit:'次/分钟'}), '最多 60 次/分钟');
  assert.equal(settingRangeLabel({min:0,max:null}), '至少 0');
  assert.equal(settingRangeLabel({min:null,max:null}), '');
  assert.equal(settingRangeLabel({...body,min:MB,max:null}), '至少 1 MB');
  assert.equal(settingRangeLabel({...body,min:1,max:null}), '大于 0 MB');
  assert.equal(settingRangeLabel({...body,min:1}), '大于 0 MB，最多 16 MB');
});

function runtime() {
  const source = fs.readFileSync(new URL('./SettingsView.vue', import.meta.url), 'utf8');
  const script = source.split('<script setup>')[1].split('</script>')[0].replace(/^import .*;\n/gm, '');
  const calls = [], errors = [], success = [];
  let stored = 2 * MB;
  const ctx = vm.createContext({...Vue, authMode: Vue.ref('controller'), onMounted:()=>{}, applyFeatureSettings:()=>{},
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

test('Webhook nullable settings start clean, retain null meaning, and leave zero distinct', () => {
  const r = runtime();
  r.run("testSpec = {path:'webhooks.retention.idempotencyDays',kind:'int',title:'防重复记录'}; specs.value = {[testSpec.path]:testSpec}; values.value = {[testSpec.path]:null}; hydrateDraft();");
  assert.equal(r.run('isDirty(testSpec)'), false);
  assert.equal(r.run('isEditing(testSpec)'), false);
  assert.equal(r.run('displayedValue(testSpec)'), '不自动清理');
  r.run('draft[testSpec.path] = 0');
  assert.equal(r.run('isDirty(testSpec)'), true);
  r.run('reset(testSpec)');
  assert.equal(r.run('isDirty(testSpec)'), false);
});

test('interpreter path comes from server and keeps its full tooltip value', () => {
  const r = runtime();
  r.run("webhookEnvironment.value = {runtimes:[{runtime:'python',path:'/opt/long-server-path/openbear/.venv/bin/python'}]}");
  assert.equal(r.run("interpreterPath({path:'webhooks.scripts.pythonPath'})"), '/opt/long-server-path/openbear/.venv/bin/python');
  assert.equal(r.run("inputPlaceholder({path:'webhooks.retention.payloadDays',kind:'int',nullable:true})"), '留空不自动清理');
});

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
