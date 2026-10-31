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
  const props = reactive({title:'测试会话',titleIdentity:'chat',conversationPath:'OpenBear',running:false,runStartedAt:0,status:'就绪',tokensText:'128.4K',tokensDetail:'输入、输出及缓存明细',cachePercentText:'80.0%',durationMs:229000,costText:'$0.0832',...overrides});
  const script = header.scriptSetup.content.replace(/^import .*;\n/gm, '');
  const ctx = vm.createContext({computed,defineProps:()=>props,defineEmits:()=>()=>{},...helpers});
  vm.runInContext(script, ctx);
  return proxyRefs(vm.runInContext('({props,pathText,tokenValue,tokenUnit,durationParts,runStatusText,fmtElapsedClockMs})', ctx));
}
async function renderHeader(overrides) {
  const state = headerState(overrides), render = compile(header.template.content);
  state.$slots = {};
  const app = createSSRApp({render(){return render.call(this, state, []);}});
  app.component('AnimatedConversationTitle', {props:['text'],render(){return h('span', this.text);}});
  app.directive('elapsed', {getSSRProps:()=>({})});
  return renderToString(app);
}

test('desktop header renders path before animated title, five readable values including cache percentage and no decoration icons', async () => {
  const html = await renderHeader({title:'长标题',conversationPath:'工程 / OpenBear'});
  assert.ok(html.indexOf('/工程/OpenBear') < html.indexOf('长标题'));
  for (const label of ['运行状态','总 Tokens','缓存命中','总耗时','总花费']) assert.ok(html.includes(label));
  assert.equal((html.match(/class="header-metric"/g)||[]).length, 5);
  assert.match(html, /80\.0%/);
  assert.match(html, /会话累计读缓存 Tokens \/ 累计完整输入 Tokens/);
  assert.match(html,/128\.4<span class="header-unit">K/);
  assert.match(html,/3<span class="header-unit">分/);
  assert.match(html,/49<span class="header-unit">秒/);
  assert.match(html,/\$0\.0832/);
  assert.match(html,/title="输入、输出及缓存明细"/);
  assert.doesNotMatch(html, /<svg|header-orb|header-run-clock/);
});

test('header preserves missing cache data and displays a measured zero hit rate', async () => {
  for (const cachePercentText of ['—', '0.0%', '92.4%']) {
    const html = await renderHeader({cachePercentText});
    assert.ok(html.includes(`<span class="header-number">${cachePercentText}</span>`));
  }
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

test('phone run line puts the run clock before the current phase under the title, and is absent while idle', async () => {
  const running = await renderHeader({running:true,status:'工具执行中'});
  const line = running.match(/<div class="header-mobile-run">([\s\S]*?)<\/div>/);
  assert.ok(line, 'the run line renders while running');
  assert.ok(running.indexOf('header-title-row') < running.indexOf('header-mobile-run'), 'the line sits below the title');
  assert.match(line[1], /<i aria-hidden="true"><\/i><span class="header-mobile-run-clock"[^>]*>00:00<\/span><span class="header-mobile-run-sep" aria-hidden="true">·<\/span><span class="header-mobile-run-status" role="status">工具执行中<\/span>/);
  assert.match(header.template.content, /class="header-mobile-run-clock"[^>]*v-elapsed="\{ startAt: props.runStartedAt, active: props.running, format: 'hours-clock', fallback: '00:00' \}"/, 'same start as the desktop clock');
  for (const status of ['就绪', '']) assert.match(await renderHeader({running:true,status}), /class="header-mobile-run-status" role="status">运行中</);
  assert.doesNotMatch(await renderHeader({status:'已停止'}), /header-mobile-run/);
});

test('path, duration and clock formatting covers nested directories, missing values and minute boundaries', () => {
  for (const [input, expected] of [['OpenBear','/OpenBear'],['项目 / OpenBear','/项目/OpenBear'],['/OpenBear','/OpenBear'],['临时会话','/临时会话'],['','—']]) {
    assert.equal(helpers.conversationPathText(input), expected);
  }
  for (const [ms, expected] of [[0,'00:00'],[42999,'00:42'],[60000,'01:00'],[3601000,'60:01']]) assert.equal(helpers.fmtElapsedClockMs(ms), expected);
  for (const [ms, expected] of [[0,'00:00'],[-5,'00:00'],[NaN,'00:00'],[83000,'01:23'],[3599999,'59:59'],[3600000,'1:00:00'],[3735000,'1:02:15'],[36061000,'10:01:01']]) assert.equal(helpers.fmtElapsedHoursClockMs(ms), expected);
  const parts = ms => JSON.parse(JSON.stringify(helpers.headerDurationParts(ms)));
  assert.deepEqual(parts(59999),[{value:'1',unit:'分'},{value:'0',unit:'秒'}]);
  assert.deepEqual(parts(0),[{value:'—',unit:''}]);
  assert.deepEqual(parts(42),[{value:'42',unit:'ms'}]);
});

test('shared elapsed directive uses header clock only when requested, updates and releases its ticker', () => {
  let now = 100000, tick, timers = 0, clears = 0;
  const ctx = vm.createContext({...helpers,Date:{now:()=>now},window:{setInterval:fn=>{tick=fn;timers++;return 1;},clearInterval:()=>{clears++;}}});
  vm.runInContext(read('./elapsedDirective.js').replace(/^import .*;\n/, '').replace('export const vElapsed', 'var vElapsed'),ctx);
  const clock = {}, legacy = {}, hours = {}, unknown = {}, directive = ctx.vElapsed;
  directive.mounted(clock,{value:{startAt:58000,active:true,format:'clock'}});
  directive.mounted(legacy,{value:{startAt:58000,active:true}});
  directive.mounted(hours,{value:{startAt:58000,active:true,format:'hours-clock'}});
  directive.mounted(unknown,{value:{startAt:58000,active:true,format:'toString'}});
  assert.equal(clock.textContent,'00:42'); assert.equal(legacy.textContent,'42.0s'); assert.equal(hours.textContent,'00:42'); assert.equal(unknown.textContent,'42.0s'); assert.equal(timers,1);
  now += 18000; tick(); assert.equal(clock.textContent,'01:00'); assert.equal(legacy.textContent,'1m00s'); assert.equal(hours.textContent,'01:00');
  now += 3600000; tick(); assert.equal(clock.textContent,'61:00'); assert.equal(hours.textContent,'1:01:00');
  directive.beforeUnmount(hours); directive.beforeUnmount(unknown);
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
    defineExpose: value=>{ctx.exposed=value;},openRootMenu(){},showProperties(){},revealDraft(){},revealFolder(){},forgetConversation(){}});
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

test('header retains full path without a title menu while giving the mobile title its own truncation row', async () => {
  const html = await renderHeader({title:'需要完整保留的长会话标题',conversationPath:'工程 / OpenBear / 界面优化'});
  assert.match(html, /title="\/工程\/OpenBear\/界面优化" aria-label="所在目录 \/工程\/OpenBear\/界面优化"/);
  assert.match(html, /header-title-row flex min-w-0 items-center gap-2/);
  assert.match(html, /title="需要完整保留的长会话标题"/);
  assert.doesNotMatch(header.template.content, /header-properties|el-dropdown/);
  assert.match(header.template.content, /<slot name="mobile-actions"\/>/);
});
