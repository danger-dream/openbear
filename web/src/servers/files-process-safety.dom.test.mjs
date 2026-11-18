import '../domTestEnv.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import {h, ref, nextTick} from 'vue';
import 'element-plus';
import {registerVueLoader} from '../testing/vueLoader.mjs';
import {waitUntil} from '../testing/domPage.mjs';
registerVueLoader({stubs: {'../views/consoleView/contextEditor/CodeEditor.vue': new URL('./testing/fileEditorFake.mjs', import.meta.url).href}});
const {mountApp} = await import('../testing/mountApp.mjs');
const {Api} = await import('../api.js');
const FileBrowser = (await import('./FileBrowser.vue')).default;
const FileViewer = (await import('./FileViewer.vue')).default;
const ProcessTop = (await import('./ProcessTop.vue')).default;
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };
const entry = {name: 'same.tar.gz', type: 'file', size: 8, mtime: 1, mode: '4755'};
const listing = path => ({ok: true, path, entries: [{...entry}], truncated: false});

for (const [action, dialog, submit, expected] of [
  ['删除', '删除', '删除', {op: 'delete', paths: ['/a/same.tar.gz']}],
  ['重命名…', '重命名', '确定', {op: 'rename', path: '/a/same.tar.gz', name: 'renamed.tgz'}],
  ['修改权限…', '修改权限', '修改', {op: 'chmod', paths: ['/a/same.tar.gz'], mode: '4755', recursive: false}],
  ['压缩…', '压缩', '压缩', {op: 'compress', paths: ['/a/same.tar.gz'], name: 'same.tar.gz', format: 'tar.gz'}],
  ['解压…', '解压', '解压', {op: 'extract', path: '/a/same.tar.gz', dest: '/a/same'}],
]) {
  test(`file ${action}: confirmation freezes server/path while navigation resolves`, async t => {
    const nav = deferred(), server = ref('original'), calls = [], polls = [];
    t.mock.method(Api, 'serverFiles', async (sid, path) => path === '/b' ? nav.promise : listing(path));
    t.mock.method(Api, 'serverFileOp', async (sid, data) => { calls.push({sid, data}); return {ok: true, job: {id: 'job-1', state: 'running'}}; });
    t.mock.method(Api, 'serverFileOpJob', async (sid, id) => { polls.push({sid, id}); return {ok: true, job: {id, state: 'done'}}; });
    const {page} = mountApp(t, {render: () => h(FileBrowser, {serverId: server.value, start: '/a'})}, {width: 390});
    await page.locator('.fb-more').waitFor();
    await page.getByRole('textbox', {name: '路径'}).fill('/b');
    await page.getByRole('textbox', {name: '路径'}).press('Enter');
    await page.locator('.fb-more').click();
    await page.getByRole('menuitem', {name: action, exact: true}).click();
    const panel = page.getByRole('dialog', {name: dialog});
    await panel.waitFor();
    nav.resolve(listing('/b'));
    await waitUntil(async () => (await page.getByRole('textbox', {name: '路径'}).inputValue()) === '/b');
    if (action === '重命名…') await panel.locator('input').fill('renamed.tgz');
    // Switching the enclosing server while the confirmation is open must not
    // move either the mutation or its background-job polling to that server.
    server.value = 'another';
    await nextTick();
    await panel.getByRole('button', {name: submit, exact: true}).click();
    await waitUntil(() => polls.length > 0);
    assert.deepEqual(calls, [{sid: 'original', data: expected}]);
    assert.deepEqual(polls, [{sid: 'original', id: 'job-1'}]);
  });
}

test('file navigation ignores older responses; create freezes its directory before prompting', async t => {
  const slow = deferred(), fast = deferred(), calls = [];
  t.mock.method(Api, 'serverFiles', async (sid, path) => path === '/slow' ? slow.promise : path === '/fast' ? fast.promise : listing(path));
  t.mock.method(Api, 'serverFileOp', async (sid, data) => { calls.push({sid, data}); return {ok: true, job: {id: 'new', state: 'done'}}; });
  const {page} = mountApp(t, {render: () => h(FileBrowser, {serverId: 'original', start: '/a'})});
  await page.locator('.fb-more').waitFor();
  const input = page.getByRole('textbox', {name: '路径'});
  await input.fill('/slow'); await input.press('Enter');
  await page.getByRole('button', {name: '新建文件夹', exact: true}).click();
  const prompt = page.getByRole('dialog', {name: '新建文件夹'});
  await prompt.locator('input').fill('new-dir');
  await input.fill('/fast'); await input.press('Enter');
  fast.resolve(listing('/fast'));
  await waitUntil(async () => (await input.inputValue()) === '/fast');
  slow.resolve(listing('/slow'));
  await nextTick();
  await prompt.getByRole('button', {name: '确定'}).click();
  await waitUntil(() => calls.length === 1);
  assert.deepEqual(calls[0], {sid: 'original', data: {op: 'mkdir', path: '/a', name: 'new-dir'}});
  assert.equal(await input.inputValue(), '/fast');
});

test('viewer save keeps the opened server/path across conflict confirmation', async t => {
  const server = ref('original'), path = ref('/a/note.txt'), writes = [];
  t.mock.method(globalThis, 'fetch', async () => new Response('old text'));
  t.mock.method(Api, 'serverFiles', async () => ({ok: true, entries: [{name: 'note.txt', mtime: 2, size: 8}]}));
  t.mock.method(Api, 'uploadServerFile', async (sid, target, blob, options) => { writes.push({sid, target, options}); });
  const {page} = mountApp(t, {render: () => h(FileViewer, {serverId: server.value, path: path.value, entry: {name: 'note.txt', mtime: 1, size: 8}, phone: true})}, {width: 390});
  await page.getByRole('textbox', {name: '编辑 note.txt'}).fill('new text');
  await page.getByRole('button', {name: '保存', exact: true}).click();
  const confirm = page.getByRole('dialog', {name: '文件已变化'});
  await confirm.waitFor();
  server.value = 'another'; path.value = '/b/note.txt'; await nextTick();
  await confirm.getByRole('button', {name: '覆盖保存'}).click();
  await waitUntil(() => writes.length === 1);
  assert.deepEqual(writes[0], {sid: 'original', target: '/a/note.txt', options: {overwrite: true}});
});

const process = identity => ({pid: 42, identity, name: 'same-name', cpu: 1, rss: 10});
test('process confirmation and forced retry stay bound to startup identity; reused PID loses old state', async t => {
  let identity = 'epoch:42:first';
  const kills = [];
  t.mock.method(Api, 'serverProcesses', async (sid, sort) => ({sort, count: 1, processes: [process(identity)]}));
  t.mock.method(Api, 'killServerProcess', async (sid, data) => { kills.push({sid, data}); return {exited: false}; });
  const server = {id: 'original', node: {info: {platform: 'linux'}, caps: {exec: true}}};
  const {page} = mountApp(t, {render: () => h(ProcessTop, {server})});
  await page.getByRole('button', {name: '结束 same-name（PID 42）'}).click();
  await page.getByRole('button', {name: '确认结束'}).click();
  await page.getByRole('button', {name: '强制结束'}).click();
  await waitUntil(() => kills.length === 2);
  assert.deepEqual(kills.map(c => c.data), [false, true].map(force => ({pid: 42, identity: 'epoch:42:first', name: 'same-name', force})));
  identity = 'epoch:42:second';
  document.dispatchEvent(new Event('visibilitychange'));
  await page.getByRole('button', {name: '结束 same-name（PID 42）'}).waitFor();
  assert.equal(await page.getByRole('button', {name: '强制结束'}).count(), 0);
  await page.getByRole('button', {name: '结束 same-name（PID 42）'}).click();
  identity = 'epoch:42:third';
  document.dispatchEvent(new Event('visibilitychange'));
  await page.getByRole('button', {name: '结束 same-name（PID 42）'}).waitFor();
  assert.equal(await page.getByRole('button', {name: '确认结束'}).count(), 0);
  assert.equal(kills.length, 2);
});

test('old process lists cannot pretend to support identity-safe termination', async t => {
  t.mock.method(Api, 'serverProcesses', async () => ({sort: 'cpu', count: 1, processes: [process(undefined)]}));
  const kill = t.mock.method(Api, 'killServerProcess', async () => assert.fail('old node must not receive kill'));
  const {page} = mountApp(t, {render: () => h(ProcessTop, {server: {id: 'old', node: {caps: {exec: true}}}})}, {width: 390, dark: true});
  const end = page.getByRole('button', {name: '结束 same-name（PID 42）'});
  await end.waitFor();
  assert.equal(await end.isDisabled(), true);
  await page.getByText('节点未提供部分进程的启动身份，已禁用结束操作；旧节点请先更新。').waitFor();
  assert.equal(kill.mock.callCount(), 0);
});
