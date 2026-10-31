import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as Vue from 'vue';
import {parse, compileTemplate, compileStyle} from '@vue/compiler-sfc';
import {parse as parseJs} from '@babel/parser';
import {settingDisplayValue, settingStorageValue, settingRangeLabel} from './settingsDisplay.js';

const path = 'contextManagement.toolResultArtifactTokens';
const source = fs.readFileSync(new URL('./SettingsView.vue', import.meta.url), 'utf8');
const descriptor = parse(source).descriptor;
function page(newBackend) {
  const specs = {'agent.compactRatio': {path:'agent.compactRatio', title:'压缩触发比例', kind:'float', group:'compaction'}};
  const values = {'agent.compactRatio': .7};
  if (newBackend) {
    specs[path] = {path, title:'工具结果落盘阈值', kind:'int', group:'compaction', min:0, unit:'token'};
    values[path] = 16000;
  }
  const calls = [], errors = [];
  const Api = {
    settingsSpecs:async()=>({ok:true, domains:[{key:'agent', title:'Agent', sections:[{key:'compaction', title:'上下文压缩', paths:Object.keys(specs)}]}], specs}),
    settings:async()=>({ok:true, values:{...values}}),
    rathOptions:async()=>({models:[]}),
    updateSetting:async(key,value)=>{
      assert.ok(specs[key], 'old backend rejects unknown fields');
      calls.push({path:key,value}); values[key] = key === path ? Number(value) : value;
      return {ok:true};
    },
  };
  const setup = descriptor.scriptSetup.content;
  const ast = parseJs(setup, {sourceType:'module'});
  const code = ast.program.body.filter(n=>n.type !== 'ImportDeclaration').map(n=>setup.slice(n.start,n.end)).join('\n');
  const ctx = vm.createContext({...Vue, authMode:Vue.ref('controller'), onMounted:()=>{}, onBeforeUnmount:()=>{}, applyFeatureSettings:()=>{},
    settingDisplayValue, settingStorageValue, settingRangeLabel, Api, apiError:e=>e.message,
    ElMessage:{error:e=>errors.push(e), success:()=>{}, info:()=>{}}, ElMessageBox:{},
    window:{location:{pathname:'/settings', search:'?section=system-settings'}}, URLSearchParams});
  vm.runInContext(code, ctx);
  return {run:code=>vm.runInContext(code,ctx), calls, errors};
}

test('server-driven compression form loads/saves against the old backend without new fields', async()=>{
  const p = page(false);
  await p.run('load()');
  assert.equal(p.run(`specs.value['${path}']`), undefined);
  assert.deepEqual(Array.from(p.run('visibleSections.value.flatMap(section=>section.specs.map(spec=>spec.path))')), ['agent.compactRatio']);
  p.run("draft['agent.compactRatio'] = .6");
  assert.equal(await p.run("save(specs.value['agent.compactRatio'])"), true);
  assert.deepEqual(p.calls, [{path:'agent.compactRatio',value:.6}]);
  assert.deepEqual(p.errors, []);
});

test('new backend advertises token threshold; zero is saved and displayed as disabled', async()=>{
  const p = page(true);
  await p.run('load()');
  assert.equal(p.run(`displayedValue(specs.value['${path}'])`), '16000token');
  assert.equal(p.run(`inputPlaceholder(specs.value['${path}'])`), 'token 数，0 关闭');
  p.run(`draft['${path}'] = '0'`);
  assert.equal(await p.run(`save(specs.value['${path}'])`), true);
  assert.deepEqual(p.calls, [{path,value:'0'}]);
  assert.equal(p.run(`displayedValue(specs.value['${path}'])`), '关闭（0 token）');
  assert.equal(p.run(`isDirty(specs.value['${path}'])`), false);
  assert.deepEqual(p.errors, []);
});

test('compression control uses existing responsive/theme-aware form and compiles', ()=>{
  assert.deepEqual(compileTemplate({source:descriptor.template.content, filename:'SettingsView.vue', id:'context-settings'}).errors, []);
  assert.deepEqual(compileStyle({source:descriptor.styles[0].content, filename:'SettingsView.vue', id:'context-settings', scoped:true}).errors, []);
  assert.match(source, /@media \(max-width: 760px\)/);
  assert.match(source, /min-width: 0; overflow: hidden; overflow-wrap: anywhere/);
  assert.match(source, /\.settings-row__main \{ display: block; \}/);
  assert.match(source, /var\(--settings-muted\)/);
});
