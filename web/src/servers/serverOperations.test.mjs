// Remote operations in normal use, on the components' own script: uploads while browsing on,
// a job log answered after the page was left, an install command tried again after it failed.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as Vue from 'vue';
import {parse} from '@vue/compiler-sfc';
import {pathsFor} from './filePaths.js';
import * as jobLog from './jobLog.js';

const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return {resolve, reject, promise}; };
const flush = async () => { for (let i = 0; i < 20; i++) await Vue.nextTick(); };
function harness(path, extra = {}) {
  const scope = Vue.effectScope(), cleanups = [];
  const context = {...Vue, console, JSON, TextEncoder, defineProps: () => ({}), defineEmits: () => () => {}, onMounted: () => {}, onBeforeUnmount: fn => cleanups.push(fn),
    window: {matchMedia: () => ({matches: false, addEventListener() {}, removeEventListener() {}})},
    ElMessage: {success() {}, error() {}, info() {}, warning() {}}, ElMessageBox: {confirm: async () => {}}, ok: x => x, safeError: e => String(e?.message || e), ...extra};
  const source = parse(fs.readFileSync(new URL(path, import.meta.url), 'utf8')).descriptor.scriptSetup.content.replace(/^import[\s\S]*?;\n/gm, '');
  vm.createContext(context); scope.run(() => vm.runInContext(source, context));
  return {context, run: code => vm.runInContext(code, context), unmount() { cleanups.forEach(fn => fn()); scope.stop(); }};
}

test('files chosen together upload to the folder they were chosen in, also when overwriting', async t => {
  const props = Vue.reactive({serverId: 'S', platform: 'linux', start: '/A'}), sent = [], first = deferred();
  let asked = 0;
  const h = harness('./FileBrowser.vue', {pathsFor, defineProps: () => props, errorText: String, bytes: String, dateTime: String, noAutofill: {},
    archiveBase: x => x, kind: () => 'file', modeBits: () => [], modeFrom: () => '',
    ElMessageBox: {confirm: async () => { asked++; }},
    Api: {serverFiles: async (id, path) => ({path, entries: [], truncated: false}),
      uploadServerFile: (id, path, file, {overwrite}) => {
        sent.push([id, path, overwrite]);
        if (sent.length === 1) return first.promise;
        if (file.name === 'two.txt' && !overwrite) return Promise.reject({response: {data: {error: 'file_exists'}}});
        return Promise.resolve({});
      }}});
  t.after(() => h.unmount());
  const files = [{name: 'one.txt', size: 1}, {name: 'two.txt', size: 1}];
  const uploading = h.context.upload(files);
  await flush();
  await h.run("go('/B')");
  assert.equal(h.run('path.value'), '/B');
  first.resolve({});
  await uploading;
  assert.deepEqual(sent, [['S', '/A/one.txt', false], ['S', '/A/two.txt', false], ['S', '/A/two.txt', true]]);
  assert.equal(asked, 1);
});

test('a job log answered after the page was left does not start polling again', async t => {
  const pending = deferred(), timers = [], calls = [];
  const h = harness('./ServerJobs.vue', {...jobLog, defineProps: () => ({serverId: 'S', active: false, now: 0}), MOBILE_VIEWPORT_QUERY: '', bytes: String,
    relativeTime: String, timeLabel: String, setTimeout: (fn, ms) => { timers.push(ms); return timers.length; }, clearTimeout() {},
    Api: {serverJob: (...args) => { calls.push(args); return pending.promise; }, serverJobs: async () => ({jobs: []})}});
  h.run("open({id: 'job1'})");
  await flush();
  assert.equal(calls.length, 1);
  h.unmount();
  pending.resolve({job: {id: 'job1', state: 'running'}, log: {data: 'x', offset: 0, next: 1, size: 1, first: 0}});
  await flush();
  assert.deepEqual(timers, []);
  await h.context.follow();
  assert.equal(calls.length, 1);
});

test('a job log reopened while its old read is on the way follows once', async t => {
  const reads = [], timers = [];
  const h = harness('./ServerJobs.vue', {...jobLog, defineProps: () => ({serverId: 'S', active: false, now: 0}), MOBILE_VIEWPORT_QUERY: '', bytes: String,
    relativeTime: String, timeLabel: String, setTimeout: (fn, ms) => { timers.push(ms); return timers.length; }, clearTimeout() {},
    Api: {serverJob: () => { const d = deferred(); reads.push(d); return d.promise; }, serverJobs: async () => ({jobs: []})}});
  t.after(() => h.unmount());
  const answer = data => ({job: {id: 'job1', state: 'running'}, log: {data, offset: 0, next: data.length, size: data.length, first: 0}});
  h.run("open({id: 'job1'})"); h.run('close()'); h.run("open({id: 'job1'})");
  await flush();
  reads[1].resolve(answer('new'));
  await flush();
  reads[0].resolve(answer('old!'));
  await flush();
  assert.equal(h.run('log.value.data'), 'new');
  assert.equal(timers.length, 1);
});

test('generating an install command again after it failed does not add the server twice', async t => {
  const created = [], updated = [], enrollments = [];
  let build = false;
  const h = harness('./AddServer.vue', {defineProps: () => ({settings: {accessPoints: [{id: 'ap'}]}, groups: [], servers: []}),
    HOST_PATTERN: '', HOST_TITLE: '', USER_PATTERN: '', USER_TITLE: '', relativeTime: String, dateTime: String, noAutofill: {},
    setTimeout: () => 1, clearTimeout() {},
    Api: {createServer: async body => { created.push(body); return {item: {id: `srv${created.length}`, name: body.name}}; },
      updateServer: async (id, body) => { updated.push([id, body]); return {item: {id, name: body.name}}; },
      serverEnrollment: async id => {
        enrollments.push(id);
        if (!build) throw {response: {status: 409, data: {error: 'node_binary_missing'}}};
        return {command: 'curl …', issuedAtMs: 1};
      }}});
  t.after(() => h.unmount());
  h.run("mode.value = 'command'; form.name = 'nas'");
  await h.context.makeCommand();
  assert.equal(h.run('command.value'), null);
  await h.context.makeCommand();
  // Renamed before the next try: the same server takes the new name.
  h.run("form.name = 'home nas'");
  build = true;
  await h.context.makeCommand();
  assert.equal(created.length, 1);
  assert.deepEqual(updated.map(([id, body]) => [id, body.name]), [['srv1', 'home nas']]);
  assert.deepEqual(enrollments, ['srv1', 'srv1', 'srv1']);
  assert.equal(h.run('command.value.item.id'), 'srv1');
  // Done: the next server is a new one.
  h.run("again(); form.name = 'other'");
  await h.context.makeCommand();
  assert.equal(created.length, 2);
});
