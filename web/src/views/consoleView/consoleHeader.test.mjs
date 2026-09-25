import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {parse} from '@vue/compiler-sfc';
import {compile, computed, createSSRApp, h, proxyRefs, reactive, ref} from 'vue';
import {renderToString} from 'vue/server-renderer';
import {ledgerTokenParts} from './ledgerUsage.js';

const read = path => fs.readFileSync(new URL(path, import.meta.url), 'utf8');
const header = parse(read('./ConsoleHeader.vue')).descriptor;
const display = read('./display.js');
function between(source, start, end) {
  const a = source.indexOf(start), b = source.indexOf(end, a + start.length);
  assert.ok(a >= 0 && b > a, start);
  return source.slice(a, b);
}
const helpers = vm.createContext({});
vm.runInContext(between(display, 'export function fmtLiveElapsedMs(', 'export function fmtElapsedFromStart(').replaceAll('export ', ''), helpers);

function headerState(overrides = {}) {
  const props = reactive({title:'测试会话',titleIdentity:'chat',conversationPath:'OpenBear',running:false,runStartedAt:0,status:'就绪',tokensText:'128.4K',tokensDetail:'输入、输出及缓存明细',durationMs:229000,costText:'$0.0832',...overrides});
  const script = header.scriptSetup.content.replace(/^import .*;\n/gm, '');
  const ctx = vm.createContext({computed,defineProps:()=>props,...helpers});
  vm.runInContext(script, ctx);
  return proxyRefs(vm.runInContext('({props,pathText,tokenValue,tokenUnit,durationParts,fmtElapsedClockMs})', ctx));
}
async function renderHeader(overrides) {
  const state = headerState(overrides), render = compile(header.template.content);
  state.$slots = {};
  const app = createSSRApp({render(){return render.call(this, state, []);}});
  app.component('AnimatedConversationTitle', {props:['text'],render(){return h('span', this.text);}});
  app.directive('elapsed', {getSSRProps:()=>({})});
  return renderToString(app);
}

test('desktop header renders path before animated title, four readable values and no decoration icons', async () => {
  const html = await renderHeader({title:'长标题',conversationPath:'工程 / OpenBear'});
  assert.ok(html.indexOf('/工程/OpenBear') < html.indexOf('长标题'));
  for (const label of ['运行状态','总 Tokens','总耗时','总花费']) assert.ok(html.includes(label));
  assert.equal((html.match(/class="header-metric"/g)||[]).length, 4);
  assert.match(html,/128\.4<span class="header-unit">K/);
  assert.match(html,/3<span class="header-unit">分/);
  assert.match(html,/49<span class="header-unit">秒/);
  assert.match(html,/\$0\.0832/);
  assert.match(html,/title="输入、输出及缓存明细"/);
  assert.doesNotMatch(html, /<svg|header-orb|header-run-clock/);
});

test('running badge and current-run clock are separate from cumulative duration, idle retains status', async () => {
  const running = await renderHeader({running:true});
  assert.match(running,/header-status is-running/);
  assert.match(running,/class="header-run-clock"[^>]*>00:00/);
  assert.match(running,/49<span class="header-unit">秒/);
  const idle = await renderHeader({status:'已停止'});
  assert.match(idle,/已停止/);
  assert.doesNotMatch(idle,/header-run-clock|header-status is-running/);
});

test('path, duration and clock formatting covers nested directories, missing values and minute boundaries', () => {
  for (const [input, expected] of [['OpenBear','/OpenBear'],['项目 / OpenBear','/项目/OpenBear'],['/OpenBear','/OpenBear'],['临时会话','/临时会话'],['','—']]) {
    assert.equal(helpers.conversationPathText(input), expected);
  }
  for (const [ms, expected] of [[0,'00:00'],[42999,'00:42'],[60000,'01:00'],[3601000,'60:01']]) assert.equal(helpers.fmtElapsedClockMs(ms), expected);
  const parts = ms => JSON.parse(JSON.stringify(helpers.headerDurationParts(ms)));
  assert.deepEqual(parts(59999),[{value:'1',unit:'分'},{value:'0',unit:'秒'}]);
  assert.deepEqual(parts(0),[{value:'—',unit:''}]);
  assert.deepEqual(parts(42),[{value:'42',unit:'ms'}]);
});

test('shared elapsed directive uses header clock only when requested, updates and releases its ticker', () => {
  let now = 100000, tick, timers = 0, clears = 0;
  const ctx = vm.createContext({...helpers,Date:{now:()=>now},window:{setInterval:fn=>{tick=fn;timers++;return 1;},clearInterval:()=>{clears++;}}});
  vm.runInContext(read('./elapsedDirective.js').replace(/^import .*;\n/, '').replace('export const vElapsed', 'var vElapsed'),ctx);
  const clock = {}, legacy = {}, directive = ctx.vElapsed;
  directive.mounted(clock,{value:{startAt:58000,active:true,format:'clock'}});
  directive.mounted(legacy,{value:{startAt:58000,active:true}});
  assert.equal(clock.textContent,'00:42'); assert.equal(legacy.textContent,'42.0s'); assert.equal(timers,1);
  now += 18000; tick(); assert.equal(clock.textContent,'01:00'); assert.equal(legacy.textContent,'1m00s');
  directive.updated(clock,{value:{active:false,fallback:'00:00'}}); assert.equal(clock.textContent,'00:00');
  directive.beforeUnmount(legacy); assert.equal(clears,1);
  directive.beforeUnmount(clock); assert.equal(clears,1);
});

test('header path reads current conversation, follows cached rename/move and draft folder without requests', () => {
  const tree = read('../../components/ConversationTree.vue'), app = read('../../App.vue');
  const rootFolders = ref([{kind:'folder',folderId:'a',name:'OpenBear',path:'工程 / OpenBear'}]);
  const recentItems = ref([{kind:'conversation',conversationUuid:'chat',folderId:'a',path:'工程 / OpenBear'}]);
  const props = reactive({draftConversation:{conversationUuid:'local:new',folderId:'a'}});
  const ctx = vm.createContext({ref,computed,rootFolders,recentItems,activityItems:ref([]),branchState:reactive({}),allFolders:ref([]),props,
    defineExpose: value=>{ctx.exposed=value;},openRootMenu(){},revealDraft(){},forgetConversation(){}});
  vm.runInContext(between(tree,'function everyKnownNode()', 'function emitRows()') + between(tree,'function folderPath(', 'function invalidateBranch('), ctx);
  vm.runInContext(between(tree,'defineExpose({', 'const activityItems'),ctx);
  ctx.conversationTreeRef = ref(ctx.exposed); ctx.activeConversationUuid = ref('chat');
  vm.runInContext(between(app,'const activeConversationPath =', 'const activeConversationTitle ='),ctx);
  const path = () => vm.runInContext('activeConversationPath.value',ctx);
  assert.equal(path(),'工程 / OpenBear');
  recentItems.value[0].path = '重命名 / OpenBear'; assert.equal(path(),'重命名 / OpenBear');
  recentItems.value[0] = {...recentItems.value[0],folderId:'b',path:'移动目标'}; assert.equal(path(),'移动目标');
  ctx.activeConversationUuid.value = 'local:new'; assert.equal(path(),'工程 / OpenBear');
  rootFolders.value[0].path = '新工程 / OpenBear'; assert.equal(path(),'新工程 / OpenBear');
  props.draftConversation.folderId = ''; assert.equal(path(),'临时会话');
  ctx.activeConversationUuid.value = 'missing'; assert.equal(path(),'');
});

test('compact total includes full input plus output once, retaining input/cache breakdown in tooltip', () => {
  const source = read('./ConsoleView.vue');
  const ctx = vm.createContext({computed,ledgerTokenParts,sessionLedgerUsage:ref({input_tokens:100,output_tokens:20,cache_read_tokens:30,cache_write_tokens:5}),fmtTokens:String,tokenLine:parts=>`${parts.input}/${parts.output}/${parts.cache}`});
  vm.runInContext(between(source,'const totalTokenParts =', 'const totalDurationMs ='),ctx);
  assert.equal(vm.runInContext('totalTokensDisplay.value',ctx),'155');
  assert.match(vm.runInContext('totalTokensDetail.value',ctx),/155 Tokens.*\n135\/20\/30/);
});
