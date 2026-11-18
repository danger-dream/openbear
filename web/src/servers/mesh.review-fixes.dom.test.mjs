// 11-17 review: real component event handlers, all server/RDP boundaries in memory.
import '../domTestEnv.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import {h, nextTick} from 'vue';
import 'element-plus';
import {registerVueLoader} from '../testing/vueLoader.mjs';
registerVueLoader({stubs: {
  'ironrdp-wasm': new URL('./testing/desktopOpsRdp.mjs', import.meta.url).href,
  '@novnc/novnc': new URL('./testing/desktopOpsVnc.mjs', import.meta.url).href,
  '../views/consoleView/contextEditor/CodeEditor.vue': new URL('./testing/fileEditorFake.mjs', import.meta.url).href,
}});
const {mountApp} = await import('../testing/mountApp.mjs');
const {waitUntil, settle, sleep} = await import('../testing/domPage.mjs');
const {Api} = await import('../api.js');
const {MeshApi} = await import('./mesh/meshApi.js');
const {DdnsApi} = await import('../ddns/ddnsApi.js');
const {mesh, refresh} = await import('./mesh/meshStore.js');
const {fastListener, forwardState} = await import('./mesh/meshHelpers.js');
const ForwardEdit = (await import('./mesh/MeshForwardEdit.vue')).default;
const FastEdit = (await import('./mesh/MeshFastPortEdit.vue')).default;
const NodeFast = (await import('./mesh/MeshNodeFast.vue')).default;
const Address = (await import('./mesh/MeshAddress.vue')).default;
const FileBrowser = (await import('./FileBrowser.vue')).default;
const DesktopPane = (await import('./DesktopPane.vue')).default;
const rdp = await import('./testing/desktopOpsRdp.mjs');
const clone = v => JSON.parse(JSON.stringify(v));
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };
const all = () => ({all: true, allSubnets: true, members: [], tags: [], prefixes: []});
const forward = () => ({id: 'fw1', entry: 'a', target: 'b', listen: 443, port: 8443, host: '127.0.0.1', line: 'fast', proto: 'tcp',
  proxyProtocol: 0, allow: {prefixes: [], provinces: []}, enabled: true, note: ''});
function state() {
  return {draftVersion: 'v1', settings: {fast: {port: 52003}},
    members: ['a', 'b', 'c'].map((id, i) => ({serverId: id, name: id.toUpperCase(), platform: 'linux', role: i === 1 ? 'member' : 'relay',
      address: `100.96.0.${i+1}`, publicEndpoint: '', listen: {udp: 51999, tcp: 52000, tls: 0}, entry: {candidates: []}, removing: false})),
    fastPorts: [{id: 'p1', serverId: 'b', port: 443, proto: 'tcp', src: all(), note: ''}], forwards: [forward()],
    nodes: {}, errors: [], applied: {revision: 1}, draft: {dirty: false}, apply: {running: false}};
}
async function meshState(t, initial = state()) {
  let current = clone(initial);
  const saves = [];
  Object.assign(mesh, {work: null, data: null, errors: [], rejected: false, conflict: false, saving: false});
  t.mock.method(MeshApi, 'state', async () => clone(current));
  t.mock.method(MeshApi, 'saveDraft', async body => {
    saves.push(clone(body));
    current = {...current, ...clone(body), draft: {dirty: true}, draftVersion: `v${saves.length+1}`};
    return clone(current);
  });
  await refresh();
  return saves;
}

for (const phone of [false, true]) {
  for (const [Component, field, collection, key, original] of [
    [ForwardEdit, '目标端口', 'forwards', 'port', 8443], [ForwardEdit, '监听端口', 'forwards', 'listen', 443],
    [FastEdit, '快速端口', 'fastPorts', 'port', 443],
  ]) {
    test(`${phone ? 'phone dark' : 'desktop light'} ${field}: invalid typing never leaks a numeric prefix into another save`, async t => {
      const saves = await meshState(t);
      const id = collection === 'forwards' ? 'fw1' : 'p1';
      const {page} = mountApp(t, {render: () => h(Component, {at: {id}, phone})}, {width: phone ? 390 : 1100, dark: phone});
      await page.getByRole('textbox', {name: field, exact: true}).waitFor();
      const input = document.querySelector(`input[aria-label="${field}"]`);
      for (const value of ['6', '65', '655', '6553', '65536']) {
        input.value = value;
        input.dispatchEvent(new Event('input', {bubbles: true}));
        await nextTick();
        assert.equal(mesh.work[collection][0][key], original);
      }
      input.dispatchEvent(new Event('change', {bubbles: true}));
      await page.getByRole('radio', {name: 'UDP', exact: true}).click();
      await waitUntil(() => saves.length && !mesh.saving);
      assert.equal(saves.at(-1)[collection][0][key], original);
      assert.equal(saves.at(-1)[collection][0].proto, 'udp');
      assert.equal(input.value, '65536');
      input.value = ''; input.dispatchEvent(new Event('input', {bubbles: true})); input.dispatchEvent(new Event('change', {bubbles: true}));
      await nextTick();
      assert.equal(input.value, '');
      assert.equal(mesh.work[collection][0][key], original);
      input.value = '9001'; input.dispatchEvent(new Event('input', {bubbles: true}));
      input.dispatchEvent(new Event('change', {bubbles: true}));
      await waitUntil(() => saves.at(-1)?.[collection]?.[0]?.[key] === 9001 && !mesh.saving);
    });
  }
}

test('only fast forwards: show target/relay mapping and pin its coordinated listener', async t => {
  const initial = state(); initial.fastPorts = [];
  initial.members[1].publicEndpoint = 'gateway.test';
  initial.effectivePorts = {fast: {a: 52003, b: 52004}, forward: {port: 51082, fastPort: 51084}};
  await meshState(t, initial);
  t.mock.method(DdnsApi, 'list', async () => ({records: []}));
  const {page} = mountApp(t, {render: () => h('div', [
    h(NodeFast, {member: mesh.work.members[1], index: 1, phone: true}),
    h(Address, {member: mesh.work.members[1], online: false}),
  ])}, {width: 390, dark: true});
  await page.getByRole('textbox', {name: '快速中转监听端口', exact: true}).waitFor();
  assert.equal(document.querySelector('[aria-label="快速中转监听端口"]').placeholder, '52004');
  const mapping = page.getByRole('textbox', {name: '快速中转 对外端口'});
  await mapping.waitFor();
  await mapping.fill('62004');
  document.querySelector('[aria-label="快速中转 对外端口"]').dispatchEvent(new Event('change', {bubbles: true}));
  assert.equal(mesh.work.members[1].listen.fast, 52004);
  assert.equal(mesh.work.members[1].publicPorts.fast, 62004);
  assert.equal(fastListener(initial, initial.members[0]), true);
  assert.equal(fastListener(initial, initial.members[1]), true);
  assert.equal(fastListener(initial, initial.members[2], initial.effectivePorts.fast), false, 'known incapable relay is not presented as listening');
  initial.forwards[0].enabled = false;
  assert.equal(fastListener(initial, initial.members[1]), false);
  initial.forwards[0].enabled = true; initial.members[0].removing = true;
  assert.equal(fastListener(initial, initial.members[1]), false);
});

test('disable is a draft edit, not an implicit apply or a false stopped runtime', async t => {
  const initial = state();
  initial.nodes.a = {state: 'ready', appliedRevision: 1, summary: {forward: {forwards: [{id: 'fw1', listen: ['tcp [::]:443'], active: 1, errors: []}]}}};
  const saves = await meshState(t, initial);
  t.mock.method(MeshApi, 'apply', async () => assert.fail('editing must never apply the mesh'));
  const {page} = mountApp(t, {render: () => h(ForwardEdit, {at: {id: 'fw1'}, phone: true})}, {width: 390});
  await page.getByRole('switch', {name: '启用'}).click();
  await waitUntil(() => saves.length && !mesh.saving);
  assert.equal(saves[0].forwards[0].enabled, false);
  await page.getByText('草稿停用 · 上报仍在监听', {exact: true}).waitFor();
  assert.equal(document.body.textContent.includes('已停用：入口不监听这个端口'), false);
  const disabled = {...forward(), enabled: false};
  assert.deepEqual(forwardState(initial, disabled), ['warn', '草稿停用 · 上报仍在监听']);
  initial.nodes.a.summary.forward.forwards = [];
  initial.draft.dirty = true;
  assert.match(forwardState(initial, disabled)[1], /待应用/);
  initial.draft.dirty = false;
  assert.deepEqual(forwardState(initial, disabled), ['off', '已停用']);
  initial.nodes.a.state = 'offline';
  assert.match(forwardState(initial, disabled)[1], /确认/);
});

test('new-file refresh overtaken by another directory never opens its same-named file', async t => {
  const slow = deferred(), calls = []; let lists = 0;
  const listing = path => ({ok: true, path, entries: [{name: 'note.txt', type: 'file', size: 0, mode: '644', mtime: 1}], truncated: false});
  t.mock.method(Api, 'serverFiles', async (sid, path) => path === '/a' && ++lists === 2 ? slow.promise : listing(path));
  t.mock.method(Api, 'serverFileOp', async (sid, data) => { calls.push({sid, data}); return {ok: true, job: {id: 'new', state: 'done'}}; });
  t.mock.method(globalThis, 'fetch', async () => new Response('fixture'));
  const {page} = mountApp(t, {render: () => h(FileBrowser, {serverId: 'original', start: '/a'})}, {width: 390});
  await page.locator('.fb-more').waitFor();
  await page.getByRole('button', {name: '新建文件', exact: true}).click();
  const prompt = page.getByRole('dialog', {name: '新建文件'});
  await prompt.locator('input').fill('note.txt');
  await prompt.getByRole('button', {name: '确定', exact: true}).click();
  await waitUntil(() => lists === 2);
  const input = page.getByRole('textbox', {name: '路径'});
  await input.fill('/b'); await input.press('Enter');
  await waitUntil(async () => await input.inputValue() === '/b');
  slow.resolve(listing('/a')); await settle();
  assert.deepEqual(calls, [{sid: 'original', data: {op: 'touch', path: '/a', name: 'note.txt'}}]);
  assert.equal(await input.inputValue(), '/b');
  assert.equal(document.querySelector('.fv'), null, 'the newly created file no longer belongs to the displayed navigation');
  assert.equal(document.querySelector('[aria-label="编辑 note.txt"]'), null);
});

test('RDP mouse chords follow buttons changes on move and retain lost-capture cleanup', async t => {
  t.mock.method(Api, 'serverDesktopConfig', async () => ({ok: true, password: 'fixture', username: 'fixture'}));
  const {unmount} = mountApp(t, {render: () => h(DesktopPane, {serverId: 'fixture', platform: 'windows', active: true})});
  await waitUntil(() => document.querySelector('.dp-stage.is-open')); await settle(); await sleep(30);
  const canvas = document.querySelector('canvas'), session = rdp.sessions.at(-1);
  canvas.setPointerCapture = () => {}; canvas.releasePointerCapture = () => {};
  const fire = (type, buttons, button) => canvas.dispatchEvent(new PointerEvent(type, {bubbles: true, cancelable: true, pointerType: 'mouse', pointerId: 7,
    buttons, button, clientX: 20, clientY: 20}));
  fire('pointerdown', 1, 0);
  fire('pointermove', 3, 2);
  fire('pointermove', 2, 0);
  fire('pointerup', 0, 2);
  assert.deepEqual(session.events.filter(e => /mouseButton/.test(e.type)).map(e => [e.type, e.args[0]]), [
    ['mouseButtonPressed', 0], ['mouseButtonPressed', 2], ['mouseButtonReleased', 0], ['mouseButtonReleased', 2],
  ]);
  fire('pointerdown', 4, 1);
  const releases = session.releases;
  canvas.dispatchEvent(new Event('lostpointercapture'));
  assert.ok(session.releases > releases);
  await unmount();
});
