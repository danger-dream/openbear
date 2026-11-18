import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as Vue from 'vue';
import {parse} from '@vue/compiler-sfc';

const moduleSource=fs.readFileSync(new URL('./promptTemplate.js',import.meta.url),'utf8').replace(/^import .*;\n/gm,'').replace(/^export /gm,'');
const menuSource=parse(fs.readFileSync(new URL('./PersonaMenu.vue',import.meta.url),'utf8')).descriptor.scriptSetup.content.replace(/^import .*;\n/gm,'');
function harness({value='7',missing=false,locked=true}={}) {
  const props=Vue.reactive({value,missing,locked,saving:false,conversationUuid:'c',draft:false});
  const ctx=vm.createContext({...Vue,props,defineProps:()=>props,defineEmits:()=>()=>{},defineExpose:()=>{},onMounted:()=>{},onBeforeUnmount:()=>{},watch:()=>{},relativeSessionTime:()=>'',Api:{}});
  vm.runInContext(moduleSource+'\n'+menuSource,ctx);
  const run=code=>vm.runInContext(code,ctx);
  run(`Object.assign(promptTemplateChoices,{active:{id:'7',name:'主模板',chars:4},items:[{id:'8',name:'写作'}],existingIds:['7','8','9'],loaded:true})`);
  return {props,run};
}
test('an explicitly selected template remains selected when promoted to main; default remains distinct',()=>{
  const h=harness();
  assert.equal(h.run("rows.value.filter(row=>row.id===selected.value).length"),1);
  assert.equal(h.run("rows.value.find(row=>row.id==='7').name"),'主模板');
  assert.equal(h.run("rows.value[0].id"),'');
  h.props.value='';
  assert.equal(h.run("rows.value.filter(row=>row.id===selected.value).length"),1);
});
test('deleted personas show attention without confusing unmarked but existing templates',()=>{
  const h=harness({value:'9'});
  assert.equal(h.run('attention.value'),false);
  h.run("promptTemplateChoices.existingIds=['7','8']");
  assert.equal(h.run('attention.value'),true);
  assert.match(h.run('label.value'),/模板已删除/);
  assert.equal(h.run('selected.value'),'9');
  assert.equal(h.run("rows.value.filter(row=>row.id===selected.value).length"),1);
});
test('frontend ID normalization has the same bounded ASCII domain as SQLite/backend',()=>{
  const h=harness();
  for(const value of ['²','١','１２','01','9'.repeat(5000),'9223372036854775808'])
    assert.equal(h.run(`normalizePromptTemplate(${JSON.stringify(value)})`),'');
  assert.equal(h.run("normalizePromptTemplate('9223372036854775807')"),'9223372036854775807');
});
