import test, {after} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {register} from 'node:module';
import {compileScript, compileStyle, compileTemplate, parse} from '@vue/compiler-sfc';
import {createRenderer, h, nextTick, reactive, ref} from 'vue';
import {conversationTimelineEntries} from './conversationTimeline.js';
import {projectOperationMessages} from '../../timelineProjection.js';
import {contextMeter, conversationWorkChunks, isInlineProcess, lastAnswerIndex, latestReasoningLine, reasoningDuration, workDurationLabel, WORK_MOTION} from './conversationWork.js';

// Execute the real Vue components in memory, without a browser/layout engine.
const files = ['ConversationWorkBlock.vue', 'ConversationProcessEvent.vue', 'ConversationRetryEvent.vue', 'WorkDisclosure.vue', 'ContextUsageMeter.vue', 'ConsoleToolEvent.vue', 'TurnList.vue', 'TurnEvent.vue'];
const sources = Object.fromEntries(files.map(f => [f, fs.readFileSync(new URL(f, import.meta.url), 'utf8')]));
const descriptors = Object.fromEntries(files.map(f => [f, parse(sources[f], {filename: f}).descriptor]));
const compiled = Object.fromEntries(files.map(f => [f, compileScript(descriptors[f], {id: f, inlineTemplate: true}).content]));
compiled['ConsoleMarkdown.vue'] = `import {h} from 'vue'; export default {props:['text'], setup:p=>()=>h('div',{class:'bear-md'},p.text)};`;
register(`data:text/javascript,${encodeURIComponent(`
  import {fileURLToPath} from 'node:url'; let sources;
  export function initialize(data) { sources = data; }
  export function load(url, context, next) {
    if (url.endsWith('.css')) return {format:'module',source:'export default {};',shortCircuit:true};
    if (url.endsWith('.vue')) return {format:'module',source:sources[fileURLToPath(url).split('/').at(-1)] || 'export default {render(){return null}}',shortCircuit:true};
    return next(url, context);
  }
`)}`, {parentURL: import.meta.url, data: compiled});
const components = Object.fromEntries(await Promise.all(files.map(async f => [f, (await import(new URL(f, import.meta.url))).default])));
const {MESSAGE_VISIBILITY} = await import('./messageVisibility.js');
const {Api} = await import('../../api.js');
const originalWindow = globalThis.window;
globalThis.window = {addEventListener() {}, removeEventListener() {}, setTimeout, clearTimeout, matchMedia: () => ({matches:false})};
after(() => { globalThis.window = originalWindow; });
const originalApi = Api.conversationOperationDetail;
let reads = 0;
Api.conversationOperationDetail = async () => { reads++; throw new Error('unexpected network'); };
after(() => {Api.conversationOperationDetail = originalApi; assert.equal(reads, 0);});

const node = (type, text = '') => ({type, text, props: {}, children: [], parent: null, style: {}, scrollTop: 0, scrollHeight: 600, clientHeight: 100,
  listeners: new Map(), addEventListener(name, fn) {this.listeners.set(name, fn);}, removeEventListener(name) {this.listeners.delete(name);}, classList: {add() {}, remove() {}}, dataset: {},
  contains(n) { return n === this || this.children.some(c => c.contains(n)); }});
const renderer = createRenderer({
  createElement: t => node(t), createText: t => node('#text', t), createComment: t => node('#comment', t),
  setText(n, t) {n.text = t;}, setElementText(n, t) {n.text = t; n.children = [];},
  patchProp(n, key, old, value) {n.props[key] = value;},
  insert(n, parent, anchor = null) {if (n.parent) this.remove(n); n.parent = parent; const i = parent.children.indexOf(anchor); parent.children.splice(i < 0 ? parent.children.length : i, 0, n);},
  remove(n) {const i = n.parent?.children.indexOf(n) ?? -1; if (i >= 0) n.parent.children.splice(i, 1); n.parent = null;},
  parentNode: n => n.parent, nextSibling: n => n.parent?.children[n.parent.children.indexOf(n) + 1] || null,
});
const walk = n => [n, ...n.children.flatMap(walk)];
const hasClass = (n, cls) => String(n.props.class || '').split(/\s+/).includes(cls);
const find = (n, cls) => walk(n).find(n => hasClass(n, cls));
const text = n => [n.text, n.props.innerHTML || '', ...n.children.map(text)].join('');
function mount(t, file, props, slots, provides = []) {
  const root = node('root');
  const app = renderer.createApp({render: () => h(components[file], props, slots)});
  for (const [key, value] of provides) app.provide(key, value);
  app.component('ElTooltip', {setup: (_, {slots}) => () => slots.default?.()});
  app.component('ElIcon', {setup: (_, {slots}) => () => slots.default?.()});
  app.component('ElImage', {render: () => null});
  app.component('ElPopover', {inheritAttrs: false, props: ['visible'], setup: (p, {slots}) => () => [slots.reference?.(), ...(p.visible ? slots.default?.() || [] : [])]});
  app.mount(root); t.after(() => app.unmount()); return root;
}
const answer = (id, content, extra = {}) => ({kind:'answer', id, message:{content, live:false}, ...extra});
const tool = (id = 'tool') => ({kind:'tool', id, calls:[{id:'call',name:'Read',arguments:'{"path":"/tmp/example.txt"}'}], result:{role:'tool',name:'Read',content:'COMPLETE-OUTPUT-END'}, operation:{opId:id,opType:'tool',status:'completed'}});
const reasoning = {kind:'answer', id:'thought', reasoningActive:false, message:{reasoning:'完整思考正文-END',content:''}};
const rowSlot = {default: ({entry}) => h('p', {'data-id':entry.event.id}, entry.event.message?.content || entry.event.message?.reasoning || entry.event.id)};

for (const file of files) test(`${file}: script, template and theme CSS compile`, () => {
  assert.deepEqual(compileTemplate({source:descriptors[file].template.content,filename:file,id:file}).errors, []);
  for (const s of descriptors[file].styles) assert.deepEqual(compileStyle({source:s.content,filename:file,id:file,scoped:s.scoped}).errors, []);
});

test('working -> complete folds only process; full final answer and manual re-expansion survive', async t => {
  const events = [reasoning, answer('progress','进度说明'), tool(), answer('final','最终答复-END')];
  const props = reactive({turn:{id:'one',events}, entries:conversationTimelineEntries(events), running:true, durationMs:413000});
  const root = mount(t, 'ConversationWorkBlock.vue', props, rowSlot);
  assert.equal(find(root,'work-heading'),undefined,'work duration and disclosure heading appear only after the run ends');
  assert.doesNotMatch(text(root), /工作中|已工作/);
  assert.match(text(root), /完整思考正文-END.*进度说明.*tool.*最终答复-END/);
  const finalNode = walk(root).find(n => n.props['data-id'] === 'final');
  props.running = false; await nextTick(); await nextTick();
  assert.match(text(root), /已工作 6 分 53 秒/);
  assert.doesNotMatch(text(root), /完整思考正文-END|进度说明/);
  assert.match(text(root), /最终答复-END/);
  assert.equal(walk(root).find(n => n.props['data-id'] === 'final'), finalNode, 'final answer is not remounted when work collapses');
  find(root,'work-heading').props.onClick(); await nextTick();
  assert.match(text(root), /完整思考正文-END.*进度说明/);
  props.entries = [...props.entries]; await nextTick();
  assert.equal(find(root,'work-heading').props['aria-expanded'],true, 'ordinary refresh cannot undo manual expansion');
});

test('historical turns start collapsed, final result remains outside even with trailing tools', t => {
  const events = [reasoning, tool(), answer('final','RESULT'), tool('after-final')];
  const root = mount(t, 'ConversationWorkBlock.vue', {turn:{events},entries:conversationTimelineEntries(events),running:false}, rowSlot);
  assert.match(text(root), /RESULT/); assert.doesNotMatch(text(root), /完整思考正文-END|after-final/);
  assert.equal(walk(root).filter(n => hasClass(n,'work-heading')).length,1);
});

test('interruption/progress-only runs do not fabricate a final answer or silently hide partial work', t => {
  const events = [reasoning, answer('partial','暂时没有完成',{operation:{payload:{segmentBoundary:true}}}), tool()];
  assert.equal(lastAnswerIndex(events),-1);
  const root = mount(t,'ConversationWorkBlock.vue',{turn:{events},entries:conversationTimelineEntries(events),running:false},rowSlot);
  assert.match(text(root),/暂时没有完成/); assert.equal(find(root,'work-heading').props['aria-expanded'],true);
});

test('interactive, failure, retry and Agent entries retain exact positions outside collapsible chunks', () => {
  const ui = {kind:'user_interaction',id:'ui'};
  const retry = {kind:'model_retry',id:'retry'};
  const agent = {kind:'tool',id:'agent',calls:[{name:'Agent'}],operation:{opType:'agent'}};
  const fail = answer('failed','失败说明',{failure:true});
  const events = [tool('one'),ui,reasoning,retry,agent,fail];
  const entries = conversationTimelineEntries(events);
  const chunks = conversationWorkChunks(entries,lastAnswerIndex(events));
  assert.deepEqual(chunks.flatMap(c => c.entries.map(e => e.event.id)),events.map(e => e.id));
  for (const id of ['ui','retry','agent','failed']) assert.equal(chunks.find(c => c.entries.some(e => e.event.id === id)).kind,'exposed');
  assert.equal(isInlineProcess({event:ui}),false); assert.equal(isInlineProcess({event:agent}),false);
  const completedAgent = {...agent, operation:{...agent.operation,status:'completed'}};
  assert.equal(conversationWorkChunks([{event:completedAgent,index:0}],-1)[0].kind,'work','completed Agent work belongs to the same fold; active Agents remain visible');
});

test('legacy combined reasoning/text splits presentation without mutating or duplicating answer content', () => {
  const event = answer('legacy','最终正文',{message:{content:'最终正文',reasoning:'旧版思考'}});
  const entries = conversationTimelineEntries([event]);
  assert.deepEqual(entries.map(e => e.part),['reasoning','answer']);
  assert.deepEqual(conversationWorkChunks(entries,0).map(c => c.kind),['work','exposed']);
  assert.equal(entries[0].event,event); assert.equal(entries[1].event,event);
  assert.equal(conversationTimelineEntries([answer('blank','',{message:{content:'  ',reasoning:'思考'}})]).length,1);
});

test('real TurnList obeys hidden operations and restores process visibility without leaking hidden text', async t => {
  const events = [reasoning,tool(),answer('final','VISIBLE-FINAL')];
  const hiddenIds = ref(new Set(['thought','tool']));
  const visibility = {hiddenIds,selecting:ref(false),selected:ref(new Set()),mobileMenu:ref(null),
    isHidden:event=>hiddenIds.value.has(event.id),canTarget:()=>false};
  const root = mount(t,'TurnList.vue',{turns:[{id:'turn',events}],running:true,detailKey:()=>'',isDetailOpen:()=>false,activeToolResultIndex:()=>0},null,[[MESSAGE_VISIBILITY,visibility]]);
  assert.match(text(root),/VISIBLE-FINAL/); assert.doesNotMatch(text(root),/完整思考正文-END|读取/);
  hiddenIds.value = new Set(); await nextTick();
  assert.match(text(root),/思考/); assert.match(text(root),/读取/);
});

test('running indicator in the real TurnList retains the original three-dot animation', t => {
  const event = {kind:'live_status',id:'running',persistentRunIndicator:true,active:true};
  assert.equal(isInlineProcess({event}),false);
  const root = mount(t,'TurnList.vue',{turns:[{id:'turn',events:[event]}],running:true,detailKey:()=>'',isDetailOpen:()=>false,activeToolResultIndex:()=>0});
  const indicator = find(root,'thinking-only-event');
  assert.ok(indicator, 'running status uses the unchanged TurnEvent presentation');
  const dots = find(indicator,'thinking-dots');
  assert.equal(dots.props['aria-label'],'正在思考');
  assert.equal(dots.children.filter(n => n.type === 'span').length,3);
  assert.equal(find(root,'process-waiting'),undefined);
});

test('new tool disclosure defers detail rendering and preserves complete existing result and native detail behavior', async t => {
  const event = tool();
  const root = mount(t,'ConversationProcessEvent.vue',{event});
  assert.equal(find(root,'tool-detail'),undefined); assert.match(text(root),/读取/);
  find(root,'process-summary').props.onClick(); await nextTick();
  assert.match(text(root),/COMPLETE-OUTPUT-END/); assert.ok(find(root,'tool-summary-hidden'));
  const original = mount(t,'ConsoleToolEvent.vue',{event,open:true});
  assert.equal(find(original,'tool-summary-hidden'),undefined);
  assert.match(text(original),/COMPLETE-OUTPUT-END/);
});

test('reasoning disclosure retains every character, scroll-up stops follow and returning to bottom resumes', async t => {
  const props = reactive({event:{...reasoning,reasoningActive:true,message:{...reasoning.message,reasoning:'BEGIN\n'+ '思考 '.repeat(10000)+'\nEND'}},part:'reasoning'});
  const root = mount(t,'ConversationProcessEvent.vue',props);
  assert.equal(find(root,'process-reasoning-scroll'),undefined);
  find(root,'process-summary').props.onClick(); await nextTick();
  assert.match(text(root),/BEGIN/); assert.match(text(root),/END/);
  const scroll = find(root,'process-reasoning-scroll');
  assert.equal(scroll.scrollTop,scroll.scrollHeight);
  scroll.scrollTop = 20; scroll.listeners.get('scroll')();
  scroll.scrollHeight = 900; scroll._workSync(); assert.equal(scroll.scrollTop,20);
  scroll.scrollTop = 800; scroll.listeners.get('scroll')();
  scroll.scrollHeight = 1200; scroll._workSync(); assert.equal(scroll.scrollTop,1200);
});

test('latest reasoning line is refreshed in place; duration is never invented from missing timestamps', () => {
  assert.deepEqual(latestReasoningLine('old\nnew\n '),{key:1,text:'new'});
  assert.equal(latestReasoningLine('old\nnew tokens').key,1);
  assert.deepEqual(latestReasoningLine('旧行\r\n  最新思考\t\r\n \t\r\n\u3000'),{key:1,text:'最新思考'});
  assert.equal(latestReasoningLine(' \t\r\n\u3000').text,'');
  assert.equal(reasoningDuration(reasoning),0);
  assert.equal(workDurationLabel(reasoningDuration(reasoning)),'');
  assert.equal(workDurationLabel(21000),'21 秒');
});

test('reasoning summary follows actual text width and releases replaced text observers', t => {
  const previousObserver=globalThis.ResizeObserver;
  const observers=[];
  globalThis.ResizeObserver=class {
    constructor(sync) {this.sync=sync;this.targets=new Set();observers.push(this);}
    observe(el) {this.targets.add(el);}
    unobserve(el) {this.targets.delete(el);}
    disconnect() {this.targets.clear();}
  };
  t.after(()=>{globalThis.ResizeObserver=previousObserver;});
  const root=mount(t,'ConversationProcessEvent.vue',{event:{...reasoning,reasoningActive:true},part:'reasoning'});
  const viewport=find(root,'reasoning-preview');
  const observer=observers.find(o=>o.targets.has(viewport));
  const oldText=find(root,'reasoning-preview-line');
  viewport.firstElementChild=oldText;viewport.clientWidth=200;viewport.scrollWidth=900;
  viewport._workSync();
  assert.ok(observer.targets.has(oldText));assert.equal(viewport.scrollLeft,900);
  const incoming=node('span','新行');viewport.firstElementChild=incoming;viewport.scrollWidth=140;
  viewport._workSync();
  assert.equal(observer.targets.has(oldText),false);assert.ok(observer.targets.has(incoming));
  assert.equal(viewport.dataset.overflow,'false');
  viewport.scrollWidth=1200;observer.sync();
  assert.equal(viewport.scrollLeft,1200);assert.equal(viewport.dataset.overflow,'true');
});

test('reasoning tokens and rapid newlines update one visible node immediately; completion drops the preview', async t => {
  const props=reactive({event:{...reasoning,reasoningActive:true,message:{reasoning:'第一行'}}});
  const root=mount(t,'ConversationProcessEvent.vue',props);
  const preview=find(root,'reasoning-preview-line');
  let content='第一行';
  for (let i=0;i<30;i++) {
    const latest=`实时摘要${i}`;
    content+=`\n${latest}`;
    props.event.message.reasoning=content;
    await nextTick();
    assert.equal(find(root,'reasoning-preview-line'),preview,'keep the sweep and visible text node alive');
    assert.equal(text(preview),latest,'no timer or pending-line playback');
    props.event.message.reasoning+=' 新的字符';await nextTick();
    assert.equal(text(preview),`${latest} 新的字符`);
  }
  props.event.reasoningActive=false;await nextTick();
  assert.equal(find(root,'reasoning-preview'),undefined);
  assert.equal(find(root,'work-status-sweep'),undefined);
});

for (const boundaryType of ['assistant_message','tool','model_retry']) test(`reasoning duration freezes at ${boundaryType} start through ongoing snapshots and late finalization`, async t => {
  const op={opId:'reasoning:duration',opType:'reasoning',turnUuid:'duration',displaySeq:1,status:'running',lifecycle:'active',createdAtMs:1000,updatedAtMs:9000,payload:{text:'已完成思考',complete:false}};
  const following={opId:'next:duration',opType:boundaryType,turnUuid:'duration',displaySeq:2,status:'running',lifecycle:'active',createdAtMs:11000,updatedAtMs:11000,payload:{text:'开始输出正文',name:'Bash',toolName:'Bash',arguments:'{}',complete:false}};
  const event=()=>projectOperationMessages([op,following]).flatMap(m=>m.localTimeline||[]).find(e=>e.id===op.opId);
  const props=reactive({event:event()});
  const root=mount(t,'ConversationProcessEvent.vue',props);
  for (const time of [12000,30000,90000]) {
    op.updatedAtMs=time;following.updatedAtMs=time;
    props.event=event();await nextTick();
    assert.equal(props.event.reasoningActive,false);
    assert.equal(reasoningDuration(props.event),10000);
    assert.equal(text(find(root,'process-meta')),'· 持续了 10 秒');
    assert.equal(find(root,'work-status-sweep'),undefined);
  }
  op.status='completed';op.lifecycle='terminal';op.terminalAtMs=90000;op.payload.complete=true;
  props.event=event();await nextTick();
  assert.equal(reasoningDuration(props.event),10000,'reload after the late terminal snapshot retains the same boundary');
  op.terminalAtMs=8000;props.event=event();await nextTick();
  assert.equal(reasoningDuration(props.event),7000,'an earlier real terminal time wins over the next phase');
});

test('terminal reasoning stops even when its payload complete marker lags', () => {
  const op={opId:'terminal-thought',opType:'reasoning',turnUuid:'terminal',displaySeq:1,status:'completed',lifecycle:'terminal',createdAtMs:1000,terminalAtMs:5000,payload:{text:'思考结束',complete:false}};
  const event=projectOperationMessages([op]).flatMap(m=>m.localTimeline||[]).find(e=>e.id===op.opId);
  assert.equal(event.reasoningActive,false);assert.equal(event.message.live,false);
});

test('whitespace-only reasoning has no empty preview or orphan separator', t => {
  const root=mount(t,'ConversationProcessEvent.vue',{event:{...reasoning,reasoningActive:true,message:{reasoning:' \t\r\n\u3000'}},part:'reasoning'});
  assert.equal(find(root,'reasoning-preview'),undefined);
  assert.equal(find(root,'process-separator'),undefined);
  assert.match(text(root),/正在思考/);
});

test('parameter-aware translations survive live operation projection and lightweight history restoration', async t => {
  for (const [name, args, expected] of [
    ['History', {action:'read',scope:'current',from:'end',lastTurns:6}, '查阅历史读取当前对话最近 6 轮内容'],
    ['History', {action:'read_turn',scope:'current',before:1,after:0}, '查阅历史读取当前对话的指定轮次 · 含前 1 轮'],
    ['History', {action:'read_turn',scope:'current',before:0,after:0}, '查阅历史读取当前对话的指定轮次'],
    ['Memory', {resource:'entry',action:'get',ref:'openbear'}, '记忆读取 openbear 记忆内容'],
    ['Browser', {action:'page',params:{op:'new',url:'https://example.com'}}, '浏览器新建 example.com 页面'],
    ['Browser', {action:'snapshot'}, '浏览器查看页面内容快照'],
    ['OpenBearControl', {action:'status'}, 'OpenBear 控制查看运行状态'],
  ]) {
    const raw = JSON.stringify(args);
    const op = {opId:`translated-${name}`,opType:'tool',turnUuid:'t',displaySeq:1,status:'running',lifecycle:'active',payload:{name,arguments:raw,preview:`${name}: raw action`}};
    const event = () => projectOperationMessages([op]).flatMap(m=>m.localTimeline||[]).find(e=>e.kind==='tool');
    const props = reactive({event:event()});
    const root = mount(t,'ConversationProcessEvent.vue',props);
    assert.equal(text(find(root,'process-kind')) + text(find(root,'process-preview')),expected);
    op.status='completed';op.lifecycle='terminal';op.payload.previewArguments=raw;delete op.payload.arguments;
    props.event=event();await nextTick();
    assert.equal(text(find(root,'process-kind')) + text(find(root,'process-preview')),expected);
    assert.equal(find(root,'work-status-sweep'),undefined);
  }
});

test('failed tool label shares the text baseline, while its icon remains centered', t => {
  const root=mount(t,'ConversationProcessEvent.vue',{event:{kind:'tool',calls:[{name:'Bash',arguments:JSON.stringify({description:'检查路径',command:'false'})}],operation:{status:'failed'}}});
  assert.equal(text(find(root,'process-kind')),'终端');
  assert.equal(text(find(root,'process-error')),'未成功');
  assert.equal(text(find(root,'process-preview')),'检查路径');
  assert.match(descriptors['ConversationProcessEvent.vue'].styles[0].content,/\.process-copy\s*\{[^}]*align-items:\s*baseline/);
  assert.match(descriptors['ConversationProcessEvent.vue'].styles[0].content,/\.process-icon\s*\{[^}]*align-self:\s*center/);
});

test('sliding window process row shows before and after token estimates instead of group counts', t => {
  const op={opId:'window',opType:'tool',turnUuid:'t',displaySeq:1,status:'completed',lifecycle:'terminal',payload:{name:'ContextCompaction',strategy:'sliding_window',scope:'root',compactionId:'c1',beforeEstimateTokens:200000,afterEstimateTokens:32000,removedBatches:164,retainedBatches:5,durationMs:700}};
  const event=projectOperationMessages([op]).flatMap(m=>m.localTimeline||[]).find(e=>e.kind==='tool');
  const root=mount(t,'ConversationProcessEvent.vue',{event});
  assert.equal(text(find(root,'process-preview')),'200k → 32k · 0.7s');
  assert.doesNotMatch(text(find(root,'process-copy')),/滑动窗口|Tokens|估算/);
  assert.doesNotMatch(text(find(root,'process-copy')),/移出|保留|164|完整记录/);
});

test('active tool sweep paints directly on title and description text, uses Lucide geometry, and stops on completion', async t => {
  const props = reactive({event:{kind:'tool',id:'exec',calls:[{name:'Bash',arguments:JSON.stringify({command:'npm run build',description:'编译并验证前端'})}],operation:{status:'running'}}});
  const root = mount(t,'ConversationProcessEvent.vue',props);
  const copy = find(root,'process-copy');
  assert.equal(hasClass(copy,'work-status-sweep'),false);
  assert.ok(hasClass(find(copy,'process-kind'),'work-status-sweep'));
  assert.ok(hasClass(find(copy,'process-preview'),'work-status-sweep'));
  for (const span of [find(copy,'process-kind'),find(copy,'process-preview')]) {
    assert.ok(text(span));
    assert.equal(span.children.filter(n=>!n.type.startsWith('#')).length,0,'sweep must paint its own text, not descendant elements');
  }
  assert.ok(hasClass(find(root,'process-icon'),'lucide-square-terminal'));
  assert.equal(find(root,'process-icon').props['stroke-width'],1.75);
  props.event.operation.status='completed'; await nextTick();
  assert.equal(find(root,'work-status-sweep'),undefined);
  assert.match(text(root),/终端/);
  props.event = {kind:'tool',calls:[{name:'Read',arguments:'{}'}],operation:{status:'completed'}}; await nextTick();
  assert.ok(hasClass(find(root,'process-icon'),'lucide-file-text'));
  props.event = {kind:'live_tool',name:'Bash',preview:'实时命令'}; await nextTick();
  assert.ok(hasClass(find(root,'process-icon'),'lucide-square-terminal'));
  assert.ok(hasClass(find(root,'process-kind'),'work-status-sweep'));
});

test('real Bash operation projection keeps description and animation across progress updates, then stops at completion', async t => {
  const description='保存两账户真实额度与统计基线，重启 Parrot 并等待健康检查恢复';
  const op={opId:'tool:live-bash',opType:'tool',turnUuid:'turn-live',displaySeq:10,status:'running',lifecycle:'active',createdAtMs:1000,updatedAtMs:1000,payload:{name:'Bash',toolName:'Bash',arguments:JSON.stringify({command:'echo example',description}),preview:'💻 Bash: echo example'}};
  const event=()=>projectOperationMessages([op]).flatMap(message=>message.localTimeline || []).find(event=>event.kind==='tool');
  const props=reactive({event:event()});
  const root=mount(t,'ConversationProcessEvent.vue',props);
  const checkRunning=()=>{assert.equal(text(find(root,'process-preview')),description);assert.ok(find(root,'work-status-sweep'));};
  checkRunning();
  const copy=find(root,'process-copy');
  for(const elapsed of [8,16,30]) {
    op.payload.preview=`💻 Bash 运行中 · ${elapsed}s · 1535 bytes · PID 2130038\n最近输出：live stdout`;
    op.updatedAtMs+=8000;props.event=event();await nextTick();checkRunning();
    assert.equal(find(root,'process-copy'),copy,'progress updates do not remount and restart the sweep');
    assert.doesNotMatch(text(find(root,'process-summary')),/PID|最近输出|live stdout/);
    assert.match(props.event.livePreview,/PID 2130038/,'the original detail progress is preserved');
  }
  op.status='completed';op.lifecycle='terminal';op.payload.resultText='status: ok';props.event=event();await nextTick();
  assert.equal(text(find(root,'process-preview')),description);
  assert.equal(find(root,'work-status-sweep'),undefined);
});

test('reasoning and grouped tools stop sweeping when their real execution states finish', async t => {
  const props=reactive({event:{...reasoning,reasoningActive:true},part:'reasoning'});
  const root=mount(t,'ConversationProcessEvent.vue',props);
  assert.ok(hasClass(find(root,'process-icon'),'lucide-brain'));
  assert.ok(hasClass(find(root,'reasoning-preview-line'),'work-status-sweep'));
  assert.equal(hasClass(find(root,'reasoning-preview'),'work-status-sweep'),false);
  assert.ok(hasClass(find(root,'process-kind'),'work-status-sweep'));
  assert.equal(hasClass(find(root,'process-copy'),'work-status-sweep'),false);
  props.event.reasoningActive=false; await nextTick();
  assert.equal(find(root,'work-status-sweep'),undefined);
  props.part=''; props.event={kind:'tool_group',events:[tool('done'),{kind:'tool',id:'active',operation:{status:'running'}}]}; await nextTick();
  assert.ok(hasClass(find(root,'process-kind'),'work-status-sweep'));
  props.event.events[1].operation.status='completed'; await nextTick();
  assert.equal(find(root,'work-status-sweep'),undefined);
});

test('main conversation retry row preserves both actions, pending state and completed status without changing the legacy detail row', async t => {
  const retry={kind:'model_retry',id:'retry',retry:{active:true,cancellable:true,waitId:'wait-1',attempt:2,maxAttempts:10,waitMs:5000,reason:'total_timeout'}};
  const retried=[],cancelled=[];
  const props=reactive({turns:[{id:'retry-turn',events:[tool(),retry,answer('final','FINAL')]}],running:true,detailKey:()=>'',isDetailOpen:()=>false,activeToolResultIndex:()=>0,retryActionPending:{},onRetryNow:event=>retried.push(event),onCancelRetry:event=>cancelled.push(event)});
  const root=mount(t,'TurnList.vue',props);
  assert.ok(find(root,'conversation-retry'));
  assert.match(text(root),/模型重试.*2\/10.*等待 5 秒.*等待重试/);
  find(root,'retry-action-now').props.onClick({stopPropagation(){}});
  find(root,'retry-action-cancel').props.onClick({stopPropagation(){}});
  assert.equal(retried[0].id,'retry'); assert.equal(cancelled[0].id,'retry');
  props.retryActionPending={waitId:'wait-1',action:'retry'}; await nextTick();
  assert.equal(find(root,'retry-action-now').props.disabled,true); assert.equal(find(root,'retry-action-cancel').props.disabled,true);
  assert.match(text(root),/请求中…/);
  props.turns[0].events[1].retry={...retry.retry,active:false,status:'completed'}; props.running=false; await nextTick(); await nextTick();
  assert.match(text(root),/重试成功/); assert.match(text(root),/FINAL/);
  assert.equal(find(root,'retry-actions'),undefined,'completed retry never exposes stale action buttons');
  const legacy=mount(t,'TurnEvent.vue',{event:retry,turnId:'legacy',index:0,detailKey:()=>'',isDetailOpen:()=>false,activeToolResultIndex:()=>0});
  assert.ok(find(legacy,'retry-inline-summary'),'work details retain the original retry renderer');
  assert.equal(find(legacy,'conversation-retry'),undefined);
});

test('context meter separates model window from trigger threshold and preserves unknown/over-limit readings', () => {
  const meter = contextMeter({known:true,tokens:135000,window:1000000,threshold:250000});
  assert.equal(meter.percent,13.5); assert.equal(meter.triggerPercent,54);
  assert.equal(contextMeter({known:false,tokens:500,window:1000,threshold:700}).percent,null);
  assert.equal(contextMeter({known:true,tokens:0,window:1000}).percent,0);
  assert.equal(contextMeter({known:true,tokens:1500,window:1000}).percent,150);
  assert.equal(contextMeter({known:true,tokens:1500,window:1000}).fill,100);
  assert.equal(contextMeter({known:true,tokens:135000,window:1000000,threshold:100000}).tone,'danger');
});

test('context actual hover/focus/touch handlers expose the same values and close on outside tap or session switch', async t => {
  const originalDocument = globalThis.document;
  const handlers = new Map();
  globalThis.document = {addEventListener:(n,f)=>handlers.set(n,f),removeEventListener:(n,f)=>handlers.get(n)===f&&handlers.delete(n)};
  const props = reactive({usage:{known:true,tokens:135000},contextWindow:1000000,threshold:250000,conversationUuid:'one'});
  const root = mount(t,'ContextUsageMeter.vue',props);
  t.after(()=>{globalThis.document=originalDocument;});
  assert.equal(find(root,'context-usage-content'),undefined);
  assert.match(text(find(root,'context-inline-value')),/135K \/ 250K/);
  assert.match(text(find(root,'context-inline-percent')),/54\.0%/);
  assert.equal(find(root,'context-ring-fill').props['stroke-dasharray'],'54 100');
  assert.match(find(root,'context-usage-trigger').props['aria-label'],/压缩阈值 250K，54\.0%/);
  find(root,'context-usage-trigger').props.onFocus(); await nextTick();
  assert.doesNotMatch(text(root),/13\.5%/); assert.match(text(root),/54\.0%/); assert.match(text(root),/135,000 tokens/);
  const popup=find(root,'context-usage-content');
  assert.match(text(popup.children.find(n=>n.type==='header')),/135K \/ 250K.*54\.0%/);
  assert.match(text(popup),/模型窗口1M/);
  assert.equal(find(root,'context-capacity-track').props['aria-valuenow'],54);
  assert.equal(find(root,'context-capacity-track').children[0].props.style.width,'54%');
  handlers.get('pointerdown')({target:{closest:()=>null}}); await nextTick();
  assert.equal(find(root,'context-usage-content'),undefined);
  find(root,'context-usage-trigger').props.onPointerdown({pointerType:'touch'}); await nextTick();
  assert.ok(find(root,'context-usage-content'));
  props.conversationUuid='two'; await nextTick(); assert.equal(find(root,'context-usage-content'),undefined);
  props.usage={known:false,tokens:135000}; await nextTick();
  assert.match(text(find(root,'context-inline-value')),/待实测 \/ 250K/);
  assert.equal(text(find(root,'context-inline-percent')),'—');
  props.usage={known:true,tokens:1500000}; await nextTick();
  assert.match(text(find(root,'context-inline-percent')),/600\.0%/);
  assert.equal(find(root,'context-ring-fill').props['stroke-dasharray'],'100 100');
  props.threshold=0; await nextTick();
  assert.match(text(find(root,'context-inline-value')),/1\.5M \/ 未设置/);
  assert.equal(text(find(root,'context-inline-percent')),'—');
  assert.ok(hasClass(find(root,'context-usage-trigger'),'is-unknown'));
  assert.equal(find(root,'context-ring-fill').props['stroke-dasharray'],'0 100');
  Object.assign(props,{usage:{known:true,tokens:200600},threshold:270000,contextWindow:1048576});
  for (const strategy of ['sliding_window','model_summary']) {
    props.strategy=strategy; await nextTick();
    assert.equal(text(find(root,'context-inline-value')),'200.6K / 270K');
    assert.equal(text(find(root,'context-inline-percent')),'74.3%');
    assert.ok(Math.abs(parseFloat(find(root,'context-ring-fill').props['stroke-dasharray'])-200600/270000*100)<1e-9);
    find(root,'context-usage-trigger').props.onPointerdown({pointerType:'touch'}); await nextTick();
    const popup=find(root,'context-usage-content'), progress=find(root,'context-capacity-track');
    assert.match(text(popup.children.find(n=>n.type==='header')),/200\.6K \/ 270K.*74\.3%/);
    assert.equal(popup.props['aria-label'],find(root,'context-usage-trigger').props['aria-label']);
    assert.equal(progress.props['aria-label'],'压缩阈值占用');
    assert.equal(progress.props['aria-valuenow'],200600/270000*100);
    assert.equal(progress.children[0].props.style.width,`${200600/270000*100}%`);
    assert.doesNotMatch(text(popup),/19\.1%/);
    handlers.get('pointerdown')({target:{closest:()=>null}}); await nextTick();
  }
  props.usage={known:true,tokens:0}; await nextTick();
  assert.equal(text(find(root,'context-inline-percent')),'0.0%');
  assert.equal(hasClass(find(root,'context-usage-trigger'),'is-unknown'),false);
  find(root,'context-usage-trigger').props.onFocus(); await nextTick();
  props.threshold=0; await nextTick();
  assert.equal(find(root,'context-capacity-track').props['aria-valuenow'],undefined);
  assert.equal(find(root,'context-capacity-track').children[0].props.style.width,'0%');
  assert.match(text(find(root,'context-usage-content').children.find(n=>n.type==='header')),/0 \/ 未设置.*—/);
});

test('real scroll-motion handlers preserve read anchor or folded header, honor user interruption and reject another conversation', () => {
  const source = fs.readFileSync(new URL('./ConsoleView.vue',import.meta.url),'utf8');
  const body = source.slice(source.indexOf('function captureWorkMotion('),source.indexOf('function captureScrollAnchor('));
  let markerTop=100, applied=0;
  const listeners = new Map(), marker={isConnected:true,getBoundingClientRect:()=>({top:markerTop})};
  const scroll={scrollTop:200,scrollHeight:900,addEventListener:(n,f)=>listeners.set(n,f),removeEventListener:n=>listeners.delete(n)};
  const anchor={content:{node:{}}}, auto=ref(false), uuid=ref('one');
  const c=vm.createContext({scroller:ref(scroll),activeConversationUuid:uuid,loadRequestGeneration:1,autoScrollLocked:auto,captureScrollAnchor:()=>anchor,applyReadingAnchor:a=>{assert.equal(a,anchor);applied++;},runProgrammaticScroll:f=>f()});
  vm.runInContext(body,c);
  let saved=c.captureWorkMotion({contains:()=>false}); c.restoreWorkMotion(saved); assert.equal(applied,1); c.finishWorkMotion(saved);
  saved=c.captureWorkMotion({contains:()=>true,previousElementSibling:marker}); markerTop=70; c.restoreWorkMotion(saved); assert.equal(scroll.scrollTop,170);
  listeners.get('wheel')(); markerTop=50; c.restoreWorkMotion(saved); assert.equal(scroll.scrollTop,170); c.finishWorkMotion(saved);
  auto.value=true; saved=c.captureWorkMotion({contains:()=>false}); c.restoreWorkMotion(saved); assert.equal(scroll.scrollTop,900);
  uuid.value='two'; scroll.scrollTop=123; c.restoreWorkMotion(saved); assert.equal(scroll.scrollTop,123); c.finishWorkMotion(saved); assert.equal(listeners.size,0);
});


test('disclosure keeps content through the height/fade animation, restores reading every frame, and releases timers', async () => {
  const body = descriptors['WorkDisclosure.vue'].scriptSetup.content.replace(/^import .*;\n/gm,'');
  const callbacks = new Map(), unmount = []; let frameId = 0, restored = 0, finished = 0, done = 0, complete;
  const motion = {capture: () => ({id:1}), restore: () => {restored++;}, finish: () => {finished++;}};
  const c = vm.createContext({WORK_MOTION, defineProps: () => ({open:true}), inject: () => motion,
    onBeforeUnmount: f => unmount.push(f), requestAnimationFrame: f => {callbacks.set(++frameId,f);return frameId;},
    cancelAnimationFrame: id => callbacks.delete(id), queueMicrotask, matchMedia: () => ({matches:false})});
  vm.runInContext(body,c);
  let keyframes, timing, cancelled = 0;
  const el = {style:{},scrollHeight:480,animate: (frames,options) => {keyframes=frames;timing=options;return {finished:new Promise(r=>{complete=r;}),cancel(){cancelled++;}};}};
  c.animate(el,false,()=>{done++;});
  assert.equal(done,0,'leaving content remains mounted until animation resolves');
  assert.equal(keyframes[0].height,'480px'); assert.equal(keyframes[1].height,'0px');
  assert.equal(keyframes[1].opacity,0); assert.equal(timing.duration,260); assert.equal(el.style.overflow,'hidden');
  const tick=callbacks.values().next().value; callbacks.clear(); tick(); assert.equal(restored,1);
  complete(); await Promise.resolve(); await Promise.resolve();
  assert.equal(done,1); assert.equal(callbacks.size,0); assert.equal(el.style.height,''); assert.ok(finished>0); assert.equal(cancelled,1);
  c.matchMedia=()=>({matches:true}); c.animate({style:{},scrollHeight:600,animate(){throw Error('reduced motion must not animate');}},false,()=>{done++;});
  assert.equal(done,2);
  c.matchMedia=()=>({matches:false}); c.animate(el,true,()=>{done++;});
  unmount.forEach(f=>f()); complete(); await Promise.resolve();
  assert.equal(done,2,'a cancelled/unmounted animation must not finish a replacement transition');
  assert.equal(callbacks.size,0);
});

test('small phone toolbar reserves separate tracks rather than hiding controls; reduced-motion and shared theme roles remain', () => {
  const composer=fs.readFileSync(new URL('./ConsoleComposer.vue',import.meta.url),'utf8');
  assert.match(composer,/@media \(max-width: 360px\)[\s\S]*?grid-template-columns: minmax\(0, 1fr\) 44px/);
  assert.match(composer,/\.composer-toolbar \:deep\(\.context-usage-trigger\) \{ position: absolute; left: 50%; bottom: -34px/);
  assert.match(composer,/\.composer-box \{[^}]*margin-bottom: 32px/);
  assert.match(composer,/\.composer-toolbar \.send-button \{ grid-row: 2; grid-column: 2/);
  // At 320px the shell (24), box padding/border (16) and send column (44)
  // leave 236px for four 44px actions. Context has its own reserved row below.
  assert.ok(320 - 40 - 44 >= 4 * 44);
  const css=fs.readFileSync(new URL('./conversationWork.css',import.meta.url),'utf8');
  assert.match(css,/--work-text: var\(--ob-text\)/);
  assert.match(css,/--work-panel: var\(--ob-surface\)/);
  assert.match(css,/--work-sweep-strong: var\(--ob-process-sweep-strong\)/);
  assert.match(css,/prefers-reduced-motion: reduce[\s\S]*animation-duration: 4s/);
  assert.doesNotMatch(css,/animation:\s*none/);
  assert.doesNotMatch(css,/\.work-detail\b|\.sidebar\s*\{/);
});
