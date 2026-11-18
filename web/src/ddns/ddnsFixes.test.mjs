import test from 'node:test';
import assert from 'node:assert/strict';
import {createBody, emptyForm, formParams, patchBody, previewCounts, recordToForm, rowKey} from './ddnsHelpers.js';

const params = {urls: ['https://ip.example/egress'], bindIface: 'wan1', iface: 'eth2', stun: ['stun.example:3479'], cmd: 'ip-source',
  v6Select: {mode: 'prefix', value: '2001:db8:', allowTemporary: false}, intervalS: 120};

test('B4: auto URL/STUN/NIC parameters survive TTL and backup-only edits', () => {
  const form = recordToForm({name: 'home.example.com', type: 'A', source: {kind: 'node', serverId: 'home', method: 'auto', methodAuto: true, params},
    policy: {multi: 'one', allowPrivate: false, onDrift: 'revert'}});
  form.ttl = 600; form.backupId = 'backup';
  assert.deepEqual(patchBody(form).source.params, params);
  assert.equal(patchBody(form).backup.serverId, 'backup');
  assert.deepEqual(formParams({...form, method: 'stun'}), {...params, urls: [], iface: '', cmd: ''}, 'explicit method switch clears unrelated parameters');
});

test('B3/R1: preview zone and independent whole-RRset consent reach create', () => {
  const form = emptyForm('home');
  form.multi = 'all';
  const row = {name: 'home.example.co.uk', zone: 'example.co.uk', type: 'A', accountId: 'a', action: 'takeover',
    rrset: true, requiresRrsetTakeover: true, remoteValues: ['198.51.100.1', '198.51.100.2']};
  assert.equal(previewCounts([row]).save, 0);
  assert.deepEqual(createBody(form, [row]).rows, [], 'multi=all is not consent');
  const body = createBody(form, [row], new Set([rowKey(row)]));
  assert.deepEqual(body.rows, [{name: row.name, zone: row.zone, type: 'A', accountId: 'a', confirmTakeover: true, confirmRrsetTakeover: true}]);
  assert.equal(body.policy.rrsetTakeover, undefined, 'one row consent is not spread to the rest of a batch');
  const single = {...row, action: 'same', requiresRrsetTakeover: false};
  assert.equal(createBody(form, [single]).rows[0].confirmRrsetTakeover, undefined);
});

test('R1: legacy multi=all does not check the new edit consent', () => {
  const form = recordToForm({name: 'home.example.com', type: 'A', source: {}, policy: {multi: 'all'}});
  assert.equal(Boolean(form.rrsetTakeover), false);
  form.rrsetTakeover = true;
  assert.equal(patchBody(form).policy.rrsetTakeover, true);
  form.rrsetTakeover = false;
  assert.equal(patchBody(form).policy.rrsetTakeover, false);
});
