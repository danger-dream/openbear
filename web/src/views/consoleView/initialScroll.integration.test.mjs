import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {ref, nextTick} from 'vue';
import {runGuardedConversationStateRefresh} from './terminalStateRefresh.js';

// Run the component's actual loader and unlock handler, not a parallel model of
// the scroll policy. Only HTTP and rendered scroll/anchor operations are seams.
const source = fs.readFileSync(process.env.SCROLL_TEST_SOURCE || new URL('./ConsoleView.vue', import.meta.url), 'utf8');
const between = (start, end) => {
  const a = source.indexOf(start), b = source.indexOf(end, a + start.length);
  assert.ok(a >= 0 && b > a);
  return source.slice(a, b);
};
function deferred() {
  let resolve, reject;
  const promise = new Promise((a, b) => {resolve = a; reject = b;});
  return {promise, resolve, reject};
}
function harness() {
  const calls = [], scrolls = [], applied = [], errors = [];
  const props = {conversationUuid: 'A'};
  const noop = () => {};
  const context = vm.createContext({
    ref, nextTick, props, runGuardedConversationStateRefresh,
    autoScrollLocked: ref(true), runConfigSaves: {appliedVersion: 0},
    modelOptions: ref([{}]), localModel: ref('test'),
    Api: {conversationState: (uuid) => {const item = {uuid, ...deferred()}; calls.push(item); return item.promise;}},
    applyLoadedConversationState: (data, uuid) => applied.push({uuid, tag: data.tag}),
    connectWs: async () => {},
    scrollBottom: async (options) => {await nextTick(); if (options.isCurrent()) scrolls.push('bottom');},
    captureScrollAnchor: () => ({id: 'reading-position'}),
    restoreScrollAnchor: async (_, options) => {if (options.isCurrent()) scrolls.push('anchor');},
    updateScrollerOverflow: noop, scheduleActiveTurnFromScroll: noop,
    loadOptions: async () => {}, loadLocalRunDefaults: async () => {},
    applyDefaultLocalModel: noop, resetLocalConversationState: noop,
    restoreDraftForConversation: noop, resetTransientThinking: noop,
    ElMessage: {error: (e) => errors.push(e)}, apiError: String,
  });
  vm.runInContext(`
    let componentMounted = true, loadRequestGeneration = 0, explicitUnlockAt = 0;
    ${between('const INITIAL_TIMELINE_LIMIT =', 'const LOAD_EARLIER_SCROLL_THRESHOLD =')}
    ${between('const loading = ref(false);', 'const sendPending = ref(false);')}
    ${between('function unlockAutoScroll()', 'function toggleAutoScrollLock()')}
    ${between('async function load(options = {})', 'async function deleteTurnSuffix(')}
  `, context);
  const run = code => vm.runInContext(code, context);
  return {calls, scrolls, applied, errors, props, context, run,
    load: (mode='preserve', options={}) => run(`load(${JSON.stringify({scrollMode: mode, ...options})})`),
    resolve: (index, tag='loaded') => calls[index].resolve({conversationUuid: calls[index].uuid, tag}),
  };
}

test('a resume/recovery refresh replacing first load still finishes its bottom scroll', async () => {
  const h = harness();
  const first = h.load('bottom');
  const replacement = h.load('preserve', {manageLoading: false});
  h.resolve(1, 'replacement'); await replacement;
  assert.deepEqual(h.scrolls, ['bottom']);
  assert.equal(h.run('loading.value'), false, 'replacement must also settle the inherited loading indicator');
  h.resolve(0, 'stale'); await first;
  assert.deepEqual(h.applied, [{uuid:'A',tag:'replacement'}]);
  assert.deepEqual(h.scrolls, ['bottom']);
});

test('a chain of superseding background loads retains bottom intent only until the latest succeeds', async () => {
  const h=harness();
  const first=h.load('bottom'), second=h.load('preserve'), third=h.load('preserve');
  h.resolve(1); await second;
  h.resolve(0); await first;
  assert.deepEqual(h.scrolls, []);
  h.resolve(2); await third;
  assert.deepEqual(h.scrolls, ['bottom']);
  h.run('unlockAutoScroll()');
  const refresh=h.load('preserve'); h.resolve(3); await refresh;
  assert.deepEqual(h.scrolls, ['bottom', 'anchor'], 'completed entry intent must not force later reading-position refreshes');
});

test('explicit upward/unlock intent cancels pending entry scroll rather than forcing the reader back', async () => {
  const h=harness();
  const first=h.load('bottom');
  h.run('unlockAutoScroll()');
  h.resolve(0); await first;
  assert.deepEqual(h.scrolls, []);
  assert.equal(h.context.autoScrollLocked.value, false);
  const refresh=h.load('preserve'); h.resolve(1); await refresh;
  assert.deepEqual(h.scrolls, ['anchor']);
});

test('A -> B -> A old response cannot consume or perform the new visit bottom scroll', async () => {
  const h=harness();
  const first=h.load('bottom');
  h.props.conversationUuid='B'; const b=h.load('bottom');
  h.props.conversationUuid='A'; const secondA=h.load('bottom');
  h.resolve(0,'old A'); await first;
  h.resolve(1,'old B'); await b;
  assert.deepEqual(h.scrolls, []);
  const replacement=h.load('preserve');
  h.resolve(3,'current A'); await replacement;
  h.resolve(2,'superseded A'); await secondA;
  assert.deepEqual(h.scrolls, ['bottom']);
  assert.deepEqual(h.applied, [{uuid:'A',tag:'current A'}]);
});

test('preserve refresh after completed initialization does not force bottom even when the lock is set', async () => {
  const h=harness();
  const first=h.load('bottom'); h.resolve(0); await first;
  const refresh=h.load('preserve'); h.resolve(1); await refresh;
  assert.deepEqual(h.scrolls, ['bottom']);
});

test('failed entry request retains pending bottom intent for a successful recovery without locking user reading', async () => {
  const h=harness();
  const first=h.load('bottom'); h.calls[0].reject(new Error('offline')); await first;
  const recovery=h.load('preserve'); h.resolve(1); await recovery;
  assert.deepEqual(h.scrolls, ['bottom']);
  assert.equal(h.errors.length,1);
});

test('local draft entry completes without leaking its bottom intent into a remote preserve load', async () => {
  const h=harness(); h.props.conversationUuid='local:new';
  await h.load('bottom');
  assert.deepEqual(h.scrolls, ['bottom']);
  h.props.conversationUuid='B';
  h.run('unlockAutoScroll()');
  const b=h.load('preserve'); h.resolve(0); await b;
  assert.deepEqual(h.scrolls,['bottom','anchor']);
});
