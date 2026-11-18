import '../domTestEnv.mjs';
import test from 'node:test';
import assert from 'node:assert/strict';
import {h} from 'vue';
import {openPage} from '../testing/appHarness.mjs';
import {settle} from '../testing/domPage.mjs';
const Form = (await import('./RecordForm.vue')).default;
const Detail = (await import('./RecordDetail.vue')).default;
await import('../instances/instances.css');
await import('./ddns.css');

const nodes = [{serverId: 'home', name: 'home', online: true, ddns: true, exec: true, v4: '198.51.100.7', v6: ''}];
const accounts = [{id: 'a', type: 'route53', alias: 'fixture', zones: ['example.co.uk']}];
const providers = [{type: 'route53', name: 'Route 53', options: [], caps: {rrset: true, ttlMin: 1, ttlMax: 2147483647}}];
const params = {urls: ['https://ip.example/custom'], bindIface: 'wan1', iface: 'eth2', stun: ['stun.example:3479'], cmd: '',
  v6Select: {mode: 'first', value: '', allowTemporary: false}, intervalS: 120};
const baseRecord = () => ({id: 'r', accountId: 'a', name: 'home.example.co.uk', type: 'A', zone: 'example.co.uk', remote: '198.51.100.1,198.51.100.2', desired: '198.51.100.7',
  source: {kind: 'node', serverId: 'home', method: 'auto', methodAuto: true, params}, ttl: null,
  policy: {multi: 'all', allowPrivate: false, onDrift: 'revert'}, options: {}});

async function setup(t, {width, edit = false}) {
  const sent = [], record = edit ? baseRecord() : null;
  const previewRow = {name: 'home.example.co.uk', zone: 'example.co.uk', type: 'A', accountId: 'a', accounts, action: 'takeover',
    value: '198.51.100.7', remote: '198.51.100.1,198.51.100.2', remoteCount: 1, rrset: true, requiresRrsetTakeover: true,
    remoteValues: ['198.51.100.1', '198.51.100.2']};
  const root = {render: () => h('div', {class: 'instance-ui servers-ui'}, [h(Form, {nodes, accounts, providers, record})])};
  const mounted = await openPage(t, root, {width, height: 844, handler: req => {
    sent.push({path: req.path, method: req.method, body: req.body});
    if (req.path.endsWith('/preview')) return {status: 200, json: {ok: true, rows: [previewRow], detected: {v4: {value: '198.51.100.7', method: 'url'}}}};
    return {status: 200, json: {ok: true, record: {...record, ...req.body}, records: [{id: 'new'}], skipped: []}};
  }});
  return {...mounted, sent};
}

test('R1 details: review notice and deletion consequence, stopping management remains the default', async t => {
  const rec = {...baseRecord(), state: 'rrset_review', readable: true};
  const sent = [];
  const root = {render: () => h('div', {class: 'instance-ui servers-ui'}, [h(Detail, {record: rec, nodes, accounts, providers})])};
  const {page, unmount} = await openPage(t, root, {width: 390, handler: req => {
    sent.push(req);
    return {status: 200, json: {ok: true, events: []}};
  }});
  assert.match(await page.locator('.dd-note').first().innerText(), /多值记录集待复核[\s\S]*198\.51\.100\.1[\s\S]*整体替换将移除其他地址/);
  await page.getByRole('button', {name: '删除', exact: true}).click();
  assert.match(await page.locator('.dd-note').last().innerText(), /所有现有 IP 都将移除/);
  assert.equal(await page.locator('.dd-note input[type="checkbox"]').isChecked(), false);
  await page.getByRole('button', {name: '删除解析', exact: true}).click();
  await settle();
  const deletion = sent.find(r => r.method === 'DELETE');
  assert.ok(deletion);
  assert.notEqual(deletion.url.searchParams.get('deleteRemote'), '1');
  await unmount();
});

for (const width of [390, 1440]) {
  test(`R1 create at ${width}px: warning and unchecked explicit consent gate save, preserve zone`, async t => {
    const {page, sent, unmount} = await setup(t, {width});
    await page.locator('.dd-domains').fill('home:example.co.uk');
    await page.locator('.dd-take').waitFor();
    const choice = page.locator('.dd-take input');
    assert.equal(await choice.isChecked(), false);
    assert.match(await page.locator('.dd-take').innerText(), /198\.51\.100\.1[\s\S]*198\.51\.100\.2[\s\S]*整体替换将移除其他地址[\s\S]*接管整个记录集/);
    assert.equal(await page.getByTestId('dd-save').isDisabled(), true);
    await choice.check();
    await page.getByTestId('dd-save').click();
    await settle();
    const post = sent.find(r => r.path === '/api/ddns/records' && r.method === 'POST');
    assert.ok(post);
    assert.equal(post.body.rows[0].zone, 'example.co.uk');
    assert.equal(post.body.rows[0].confirmRrsetTakeover, true);
    await unmount();
  });

  test(`B4/R1 edit at ${width}px: TTL edit preserves auto params; multi=all is not whole-set consent`, async t => {
    const {page, sent, unmount} = await setup(t, {width, edit: true});
    const consent = page.locator('.dd-take input');
    assert.equal(await consent.isChecked(), false);
    assert.match(await page.locator('.dd-preview').innerText(), /整体替换将移除其他地址/);
    await page.locator('.dd-field').filter({hasText: 'TTL'}).locator('select').selectOption('600');
    await page.getByTestId('dd-save').click();
    await settle();
    const patch = sent.find(r => r.method === 'PATCH');
    assert.ok(patch);
    assert.deepEqual(patch.body.source.params, params);
    assert.equal(patch.body.ttl, 600);
    assert.notEqual(patch.body.policy.rrsetTakeover, true);
    await consent.check();
    await page.getByTestId('dd-save').click();
    await settle();
    assert.equal(sent.filter(r => r.method === 'PATCH').at(-1).body.policy.rrsetTakeover, true);
    await unmount();
  });
}
