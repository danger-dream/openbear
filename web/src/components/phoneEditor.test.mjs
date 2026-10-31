import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as Vue from 'vue';
import { renderToString } from 'vue/server-renderer';
import { parse } from '@vue/compiler-sfc';
import { parse as jsParse } from '@babel/parser';
import { resizePhoneTextarea } from './phoneTextarea.js';

const editor = parse(fs.readFileSync(new URL('AdaptiveMdEditor.vue', import.meta.url), 'utf8')).descriptor;
const walk = nodes => (nodes || []).flatMap(n => [n, ...walk(Array.isArray(n.children) ? n.children : [])]);
function runtime(phone = true, flow = false) {
  const props = Vue.reactive({ modelValue: '原始内容\n[[ runtimeInfo.host ]]\n@mem/test', mobileFlow: flow, readOnly: false });
  const isPhone = Vue.ref(phone), events = [], cleanup = [];
  const CodeEditor = { name:'CodeEditor', props:['modelValue'], render: () => Vue.h('div') };
  const context = vm.createContext({ ...Vue, ResizeObserver: undefined, resizePhoneTextarea,
    defineOptions: () => {}, defineProps: () => props,
    defineEmits: () => (name, value) => {events.push([name, value]); props.modelValue = value;},
    defineAsyncComponent: () => CodeEditor, useAdminPhone: () => isPhone,
    onBeforeUnmount: fn => cleanup.push(fn),
  });
  const script = editor.scriptSetup.content;
  vm.runInContext(script.replace(/^import .*;\n/gm, ''), context);
  const names = jsParse(script, {sourceType:'module'}).program.body.flatMap(n => n.type === 'VariableDeclaration' ? n.declarations.map(d => d.id.name) : []);
  const attrs = { 'completion-mode':'template', square:true, 'ref-data':{mem:[{key:'test'}]} };
  context.attributeBag = attrs;
  const bindings = () => Vue.proxyRefs(vm.runInContext(`({${names.join(',')}, mobileFlow: props.mobileFlow, readOnly: props.readOnly, $attrs: attributeBag})`, context));
  return {props, isPhone, CodeEditor, events, cleanup, attrs, bindings, run: code => vm.runInContext(code, context)};
}
async function render(r) {
  let tree;
  const compiled = Vue.compile(editor.template.content);
  const app = Vue.createSSRApp({render() {tree = compiled.call(this, r.bindings(), []); return tree;}});
  app.component('MdEditor', r.CodeEditor);
  app.config.warnHandler = message => assert.fail(message);
  let failure;
  app.config.errorHandler = error => { failure = error; };
  const html = await renderToString(app);
  if (failure) throw failure;
  return {html, nodes: walk([tree])};
}

test('phone defaults to native text, keeps exact draft across code mode/rotation, desktop forwards completion props', async () => {
  const r = runtime();
  let view = await render(r);
  let input = view.nodes.find(n => n.type === 'textarea');
  assert.ok(input); assert.equal(view.nodes.some(n => n.type === r.CodeEditor), false);
  const draft = '中文输入与换行\n\n[[ helpers.toolLines(toolNames) ]]\n@doc/原文\n';
  input.props['onUpdate:modelValue'](draft);
  assert.equal(r.props.modelValue, draft);
  const buttons = view.nodes.filter(n => n.type === 'button');
  buttons[1].props.onClick();
  view = await render(r);
  assert.equal(view.nodes.some(n => n.type === 'textarea'), false);
  const code = view.nodes.find(n => n.type === r.CodeEditor);
  assert.equal(code.props.modelValue, draft);
  assert.equal(code.props['completion-mode'], 'template');
  assert.equal(code.props.square, true);
  assert.equal(code.props['ref-data'], r.attrs['ref-data']);
  code.props['onUpdate:modelValue'](draft + '尾部');
  view.nodes.filter(n => n.type === 'button')[0].props.onClick();
  view = await render(r);
  assert.ok(view.nodes.some(n => n.type === 'textarea'));
  assert.equal(r.props.modelValue, draft + '尾部');
  r.isPhone.value = false;
  view = await render(r);
  assert.equal(view.nodes.some(n => n.type === 'button'), false);
  assert.equal(view.nodes.find(n => n.type === r.CodeEditor).props.modelValue, draft + '尾部');
  r.cleanup.forEach(fn => fn());
});

test('inherited content is readonly in native and code mode; override restores editing', async () => {
  const r = runtime(); r.props.readOnly = true;
  let view = await render(r);
  let input = view.nodes.find(n => n.type === 'textarea');
  assert.equal(input.props.readonly, true);
  input.props['onUpdate:modelValue']('blocked');
  assert.equal(r.events.length, 0);
  r.run('advanced.value = true');
  view = await render(r);
  const code = view.nodes.find(n => n.type === r.CodeEditor);
  assert.equal(code.props['read-only'], true);
  code.props['onUpdate:modelValue']('blocked');
  assert.equal(r.events.length, 0);
  r.props.readOnly = false; r.run('advanced.value = false');
  view = await render(r); input = view.nodes.find(n => n.type === 'textarea');
  assert.equal(input.props.readonly, false);
  input.props['onUpdate:modelValue']('override');
  assert.equal(r.props.modelValue, 'override');
});

test('Monaco receives readonly on creation and updates it without losing parent text', async () => {
  const source = parse(fs.readFileSync(new URL('MdEditor.vue', import.meta.url), 'utf8')).descriptor.scriptSetup.content;
  const props = Vue.reactive({modelValue:'inherited',readOnly:true,language:'markdown'});
  const mounts=[],cleanups=[],events=[],updates=[]; let content,change,created;
  const model={};
  const instance={getModel:()=>model,getValue:()=>content,setValue:v=>{content=v;change?.();},updateOptions:v=>updates.push({...v}),onDidFocusEditorWidget(){},onDidChangeModelContent:fn=>{change=fn;},getPosition:()=>null,dispose(){}};
  const ctx=vm.createContext({...Vue,defineProps:()=>props,defineEmits:()=> (...args)=>events.push(args),onMounted:fn=>mounts.push(fn),onBeforeUnmount:fn=>cleanups.push(fn),
    window:{__mdCompletionRegistered:true},document:{documentElement:{}},getComputedStyle:()=>({getPropertyValue:()=>''}),editorTheme:()=>({}),isDarkTheme:()=>false,subscribeTheme:()=>()=>{},bindMobileEditorFontSize:()=>()=>{},
    monaco:{editor:{defineTheme(){},create:(_el,options)=>{created=options;content=options.value;return instance;}}}});
  const scope=Vue.effectScope(); scope.run(()=>vm.runInContext(source.replace(/^import .*;\n/gm,''),ctx));
  mounts.forEach(fn=>fn()); assert.equal(created.readOnly,true);
  props.modelValue='updated inherited'; await Vue.nextTick(); assert.equal(content,'updated inherited'); assert.equal(events.length,0);
  props.readOnly=false; await Vue.nextTick(); assert.equal(updates.at(-1).readOnly,false);
  instance.setValue('override'); assert.deepEqual(events.at(-1),['update:modelValue','override']);
  props.readOnly=true; await Vue.nextTick(); assert.equal(updates.at(-1).readOnly,true);
  cleanups.forEach(fn=>fn()); scope.stop();
});

test('textarea uses Vue composition-aware v-model and does not intercept touch/wheel scrolling', () => {
  assert.match(editor.template.content, /<textarea[^>]*v-model="model"/);
  assert.doesNotMatch(editor.template.content, /@(?:touch|pointer|wheel)|\.prevent/);
  const style = editor.styles.map(s => s.content).join('\n');
  assert.match(style, /\.phone-native-editor\s*\{[^}]*overflow-y: auto/s);
  assert.match(style, /\.is-flow \.phone-native-editor\s*\{[^}]*overflow: hidden/s);
  assert.match(style, /\.adaptive-md-editor\.is-flow\s*\{[^}]*height: auto/s);
});

test('flow editor grows/shrinks without moving the form scroll position; fixed mode leaves height to flex layout', () => {
  const parent = {scrollTop:180, parentElement:null};
  let height = '200px', measured = 1700, calls = 0;
  const style = {set height(value) {height=value; if(value==='0px') parent.scrollTop=0;}, get height(){return height;}, removeProperty(name){assert.equal(name,'height'); height='';}};
  const input = {clientWidth:300, parentElement:parent, style, get scrollHeight(){calls++; return measured;}};
  resizePhoneTextarea(input,true);
  assert.equal(height,'1702px'); assert.equal(parent.scrollTop,180);
  measured=80; resizePhoneTextarea(input,true);
  assert.equal(height,'200px'); assert.equal(parent.scrollTop,180);
  resizePhoneTextarea(input,false); assert.equal(height,'');
  input.clientWidth=0; const before=calls;
  resizePhoneTextarea(input,true); assert.equal(calls,before,'hidden preview pane must not collapse the draft');
});
