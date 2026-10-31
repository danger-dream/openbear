import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as Vue from 'vue';
import {parse, compileScript} from '@vue/compiler-sfc';
import {renderToString} from 'vue/server-renderer';
import * as policy from './promptPolicy.js';
import * as settingsDisplay from '../views/settingsDisplay.js';

const clone = value => JSON.parse(JSON.stringify(value));
const defer = () => { let resolve; const promise = new Promise(r => {resolve = r;}); return {promise, resolve}; };
const shell = {inheritAttrs:false, setup(_, {slots}) {return () => Vue.h('div', [slots.default?.(), slots.footer?.()]);}};
function harness(name, props = {}, api = {}, extra = {}) {
  const descriptor = parse(fs.readFileSync(new URL(name, import.meta.url), 'utf8')).descriptor;
  const inputs = Vue.reactive(props), events = [], mounts = [], cleanup = [], scope = Vue.effectScope();
  const bindings = compileScript(descriptor, {id:'policy-test'}).bindings;
  const components = Object.fromEntries(Object.entries(bindings).filter(([key]) => /^[A-Z]/.test(key)).map(([key]) => [key, shell]));
  const ctx = vm.createContext({...Vue, ...components, ...policy, ...settingsDisplay, authMode: Vue.ref('controller'), defineAsyncComponent:() => shell,
    defineProps:() => inputs, defineEmits:() => (name, value) => {events.push([name, value]); if (name === 'update:modelValue') inputs.modelValue = value;},
    defineOptions(){}, onMounted:fn => mounts.push(fn), onBeforeUnmount:fn => cleanup.push(fn),
    Api:api, apiError:e => e?.response?.data?.error || e.message, mobileSelectOptions:() => ({}),
    ElMessage:{success(){},error(){},info(){}}, ElMessageBox:{confirm:async()=>{}},
    window:{location:{search:'',pathname:'/settings'},addEventListener(){},removeEventListener(){}}, URLSearchParams,
    console, ...extra});
  scope.run(() => vm.runInContext(descriptor.scriptSetup.content.replace(/^import .*;\n/gm, ''), ctx));
  const run = code => vm.runInContext(code, ctx);
  const state = () => Vue.proxyRefs({...run(`({${Object.keys(bindings).filter(key => bindings[key] !== 'props').join(',')}})`), ...inputs});
  async function render() {
    let tree;
    const draw = Vue.compile(descriptor.template.content);
    const app = Vue.createSSRApp({render() {tree = draw.call(this, state(), []); return tree;}});
    for (const name of ['ElButton','ElCheckbox','ElSelect','ElOption','ElOptionGroup','ElDialog']) app.component(name, shell);
    for (const [name, component] of Object.entries(components)) app.component(name, component);
    app.config.warnHandler=()=>{};
    const html = await renderToString(app);
    const walk = nodes => (nodes || []).flatMap(node => [node, ...walk(Array.isArray(node?.children) ? node.children : [])]);
    return {html, nodes:walk([tree])};
  }
  return {run, props:inputs, events, mounts, render, ctx, descriptor, close(){cleanup.forEach(fn=>fn());scope.stop();}};
}
const defaults = policy.normalizePromptPolicy();
const inherited = {...defaults,text:'Parent [[ time.now ]]',overrideSystemPrompt:true,toolsEnabled:true,toolNames:['missing'],userMessageTemplateEnabled:true};
const policyProps = (local = defaults, extra = {}) => ({modelValue:clone(local), inheritedPolicy:clone(inherited), inheritedSourcePath:'/parent', disabled:false,folderId:'F1',conversationId:undefined,...extra});

test('empty and whitespace text keep one local editor without displaying inherited content', async () => {
  for (const text of ['', ' \n ']) {
    const h=harness('PromptPolicyEditor.vue',policyProps({...defaults,text}));
    const view=await h.render();
    assert.doesNotMatch(view.html,/Parent \[\[ time.now \]\]|上级提示词|继承提示词预览|当前整套继承|继承状态只读|TaskMemory|滑窗|框架说明|\/parent/);assert.match(view.html,/留空继承/);
    assert.equal(h.run('inherits.value'),true);assert.equal(h.run('flagsDisabled.value'),true);
    const editors=view.nodes.filter(node=>node.props?.['onUpdate:modelValue'] && node.props?.['read-only'] !== undefined);
    assert.equal(editors.length,1);
    const editor=editors[0];
    assert.equal(editor.props['model-value'],text);assert.equal(editor.props['read-only'],false);
    h.run("patch('overrideSystemPrompt', false)");assert.equal(h.events.length,0);
    editor.props['onUpdate:modelValue']('local draft');
    assert.equal(h.props.modelValue.text,'local draft');assert.equal(h.props.modelValue.overrideSystemPrompt,false);
    assert.equal(h.props.modelValue.toolNames.length,0);assert.equal(h.props.modelValue.userMessageTemplateEnabled,false);
    assert.equal(h.run('displayed.value.text'),'local draft');h.close();
  }
});

test('ordinary Markdown stays literal, tools hidden and never invokes template preview', async () => {
  let previews=0;
  const h=harness('PromptPolicyEditor.vue',policyProps({...defaults,text:'[[ time.now ]] @raw'}),{previewPromptTemplate:async()=>{previews++;}});
  const view=await h.render();assert.doesNotMatch(view.html,/使用工具|预览模板正文|注入用户消息模板|Parent|上级提示词|继承提示词预览|普通 Markdown|TaskMemory|滑窗|继承状态只读/);
  const editor=view.nodes.find(node=>node.props?.['completion-mode']==='none');assert.ok(editor);assert.equal(editor.props['model-value'],'[[ time.now ]] @raw');
  await h.run('runPreview()');assert.equal(previews,0);h.close();
});

test('override toggles and unavailable tool choices retain exact policy values', async () => {
  const h=harness('PromptPolicyEditor.vue',policyProps({...defaults,text:'custom',overrideSystemPrompt:true,toolNames:['missing']}),{promptTools:async()=>({items:[{name:'Read',source:'builtin',description:'Read file'},{name:'mcp.x',source:'mcp',description:'MCP'}]})});
  await h.run('loadTools()');const groups=clone(h.run('groups.value'));
  assert.deepEqual(groups.map(group=>group.label),['内置工具','MCP 工具','不可用的已选工具']);assert.equal(groups[2].items[0].name,'missing');
  h.run("patch('toolsEnabled',true);patch('toolNames',['missing','Read']);patch('userMessageTemplateEnabled',true)");
  assert.deepEqual(clone(h.props.modelValue),{...defaults,text:'custom',overrideSystemPrompt:true,toolsEnabled:true,toolNames:['missing','Read'],userMessageTemplateEnabled:true});
  h.run("patch('toolsEnabled',false)");assert.deepEqual(clone(h.props.modelValue.toolNames),['missing','Read']);
  const view=await h.render();assert.ok(view.nodes.some(node=>node.props?.filterable!==undefined && node.props.multiple!==undefined));
  const optionGroups=view.nodes.find(node=>node.props?.multiple!==undefined).children.default();
  assert.ok(optionGroups.length);h.close();
});

test('tools read failure preserves selected values and readonly mode rejects changes',async()=>{
  const h=harness('PromptPolicyEditor.vue',policyProps(inherited,{disabled:true}),{promptTools:async()=>{throw Error('offline');}});
  await h.run('loadTools()');assert.equal(h.run('toolsError.value'),'offline');assert.equal(h.run('groups.value[0].items[0].name'),'missing');
  h.run("patch('text','bad');patch('toolNames',[])");assert.equal(h.events.length,0);h.close();
});

test('folder and conversation previews send actual scope and policy only, never write the result into local draft',async()=>{
  for (const scope of [{folderId:'__temporary'},{conversationId:'C1',folderId:undefined}]) {
    const calls=[];const local={...inherited,text:'[[ time.now ]]'};
    const h=harness('PromptPolicyEditor.vue',policyProps(local,scope),{previewPromptTemplate:async data=>{calls.push(clone(data));return {prompt:'Rendered',params:{},output_len:8};}});
    await h.run('runPreview()');assert.deepEqual(calls,[{...Object.fromEntries(Object.entries(scope).filter(([,value])=>value!==undefined)),...local}]);
    assert.equal(h.run('preview.value'),'Rendered');assert.equal(h.props.modelValue.text,'[[ time.now ]]');assert.equal(h.events.length,0);
    const view=await h.render();assert.doesNotMatch(view.html,/TaskMemory|滑窗|框架说明|确认门禁|不保存、不运行模型/);h.close();
  }
});

test('stale previews and 422 errors leave exact drafts intact',async()=>{
  const pending=defer();const h=harness('PromptPolicyEditor.vue',policyProps(inherited),{previewPromptTemplate:()=>pending.promise});
  const preview=h.run('runPreview()');h.run("patch('text','new draft')");pending.resolve({prompt:'old response'});await preview;
  assert.equal(h.run('previewReady.value'),false);assert.equal(h.props.modelValue.text,'new draft');
  h.ctx.Api.previewPromptTemplate=async()=>{throw {response:{data:{error:'invalid_template',message:'Unclosed @if'}}};};
  await h.run('runPreview()');assert.equal(h.run('previewError.value'),'Unclosed @if');assert.equal(h.props.modelValue.text,'new draft');h.close();
});

test('PromptTemplateEditor fixes template mode, forwards mobile layout and blocks readonly updates', async()=>{
  const h=harness('PromptTemplateEditor.vue',{modelValue:'draft',readOnly:true,mobileFlow:true,square:true,completionRoots:['time']});
  let view=await h.render();const editor=view.nodes.find(node=>node.props?.['completion-mode']==='template');
  assert.ok(editor);assert.equal(editor.props.language,'markdown');assert.equal(editor.props['mobile-flow'],true);assert.equal(editor.props.square,true);assert.deepEqual(clone(editor.props['completion-roots']),['time']);
  editor.props['onUpdate:modelValue']('bad');assert.equal(h.events.length,0);h.props.readOnly=false;h.run(`update(${JSON.stringify(" \n ")})`);assert.equal(h.props.modelValue,' \n ');h.close();
});

test('impact chooser exposes cancel / only save / save and update without a default acceptance', async()=>{
  const h=harness('PromptImpactDialog.vue',{modelValue:true,action:'保存',impact:{affectedCount:3,updatableCount:1,runningCount:2,archivedCount:1}});
  const view=await h.render();assert.match(view.html,/运行中 2/);assert.equal(h.events.length,0);
  const root=view.nodes.find(node=>node.props?.title);const buttons=root.children.footer()[0].children;
  buttons.forEach(button=>button.props.onClick());assert.deepEqual(h.events,[['choose',null],['choose',false],['choose',true]]);
  root.props['onUpdate:modelValue'](false);assert.deepEqual(h.events.at(-1),['choose',null]);h.close();
});

const settingSpec={path:'userMessageTemplate.template',kind:'str',editor:'template',title:'用户消息尾部模板',defaultValue:'[⏰ 当前时间: [[ time.now ]] ([[ time.weekday ]])]'};
const settingBool={path:'userMessageTemplate.enabled',kind:'bool',defaultValue:true,title:'用户消息尾部模板开关'};
function settingsHarness(api={}) {
  const h=harness('../views/SettingsView.vue',{},api);h.ctx.templateSpec=settingSpec;h.ctx.enabledSpec=settingBool;
  h.run("specs.value={[templateSpec.path]:templateSpec,[enabledSpec.path]:enabledSpec};values.value={[templateSpec.path]:templateSpec.defaultValue,[enabledSpec.path]:true};hydrateDraft()");return h;
}
test('global template specs use template labels/defaults and preview current draft using settings endpoint',async()=>{
  const calls=[];const h=settingsHarness({previewSettingPrompt:async(path,value)=>{calls.push({path,value});return {ok:true,path,rendered:'Current time'};}});
  assert.equal(h.run('isPromptEditorSpec(templateSpec)'),true);assert.deepEqual(clone(h.run('userMessageTemplateCompletionRoots')),['time','message','conversation','runtimeInfo.model','runtimeInfo.host','runtimeInfo.os','folderWorkspaceDir']);assert.match(h.descriptor.template.content,/:completion-roots="templateCompletionRoots\(spec.path\)"/);assert.deepEqual(clone(h.run("templateCompletionRoots('userMessageTemplate.template')")),clone(h.run('userMessageTemplateCompletionRoots')));assert.equal(h.run("templateCompletionRoots('agent.namingPrompt')"),null);assert.equal(h.run('draft[enabledSpec.path]'),true);
  assert.equal(h.run("promptVariableLabel('time.now',templateSpec)"),'[[ time.now ]]');assert.equal(h.run("promptVariableLabel('time', {editor:'prompt'})"),'{time}');
  h.run("draft[templateSpec.path]='[[ message.id ]]';editingPromptPath.value=templateSpec.path");await h.run('previewPrompt(templateSpec)');
  assert.deepEqual(calls,[{path:settingSpec.path,value:'[[ message.id ]]'}]);assert.equal(h.run('promptPreviewText.value'),'Current time');assert.equal(h.run('draft[templateSpec.path]'),'[[ message.id ]]');
  assert.equal(h.run('promptPreviewIsTemplate.value'),true);assert.match(h.descriptor.template.content,/<PromptTemplateEditor v-if="spec.editor === 'template'"/);h.close();
});
test('global template save/restore and switch requests preserve drafts on error and across other setting saves',async()=>{
  const values={[settingSpec.path]:settingSpec.defaultValue,[settingBool.path]:true},calls=[];
  const h=settingsHarness({updateSetting:async(path,value)=>{calls.push({path,value});values[path]=value;return {ok:true};},settings:async()=>({values})});
  h.run("draft[templateSpec.path]='unsaved template'");await h.run('toggleBool(enabledSpec)');assert.equal(h.run('draft[templateSpec.path]'),'unsaved template');assert.equal(calls[0].value,false);
  h.ctx.Api.updateSetting=async()=>{throw Error('bad template');};await h.run('save(templateSpec)');assert.equal(h.run('draft[templateSpec.path]'),'unsaved template');
  h.ctx.Api.updateSetting=async(path,value)=>{calls.push({path,value});values[path]=value;return {ok:true};};
  await h.run('useBuiltinPrompt(templateSpec)');assert.deepEqual(calls.at(-1),{path:settingSpec.path,value:settingSpec.defaultValue});
  h.run("draft[templateSpec.path]=''");assert.equal(h.run('isDirty(templateSpec)'),true);await h.run('save(templateSpec)');assert.equal(calls.at(-1).value,'');assert.equal(h.run('isDirty(templateSpec)'),false);h.close();
});

test('API contract uses named URLs and exact bodies; setting preview has no legacy formatting variables by default',async()=>{
  const source=fs.readFileSync(new URL('../api.js',import.meta.url),'utf8').replace(/^import .*;\n/gm,'').replaceAll('export ', '');
  const calls=[];const api={interceptors:{request:{use(){}},response:{use(){}}}};
  for(const method of ['get','post','put','patch'])api[method]=async(url,body)=>{calls.push({method,url,body});return {data:{ok:true}};};
  const ctx=vm.createContext({axios:{create:()=>api}});vm.runInContext(source,ctx);
  await vm.runInContext("Api.promptTools();Api.previewPromptTemplate({folderId:'F1',text:'draft'});Api.conversationPropertiesImpact('C/1',{contextText:''});Api.previewSettingPrompt('userMessageTemplate.template','[[ time.now ]]')",ctx);
  assert.deepEqual(clone(calls),[{method:'get',url:'/prompt-tools'},{method:'post',url:'/prompt-template/preview',body:{folderId:'F1',text:'draft'}},{method:'post',url:'/conversations/C%2F1/properties/impact',body:{contextText:''}},{method:'post',url:'/settings/prompt-preview',body:{path:settingSpec.path,value:'[[ time.now ]]'}}]);
});
