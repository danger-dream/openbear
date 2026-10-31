import test from 'node:test';
import assert from 'node:assert/strict';
import { automaticRow, batchMessage, rangeSelection, selectableRow } from './conversationTreeSelection.js';
import { defaultConfig, submittedConfig } from './webhooks/webhookConfig.js';
import { draftOf } from './cron/cronConfig.js';

const row = (id, extra = {}) => ({ kind: 'conversation', id, conversationUuid: id, ...extra });

test('only persisted conversations are selectable and automatic origins are webhook or cron', () => {
  assert.equal(selectableRow(row('a')), true);
  assert.equal(selectableRow(row('local:new', { local: true })), false);
  assert.equal(selectableRow({ kind: 'folder', folderId: 'F' }), false);
  assert.deepEqual(['webhook', 'cron', 'user', 'collab'].map(origin => automaticRow(row('x', { origin }))), [true, true, false, false]);
});

test('shift range follows displayed order in either direction and skips folders and drafts', () => {
  const rows = [row('a'), { kind: 'folder', folderId: 'F' }, row('b'), row('draft', { local: true }), row('c'), row('b', { recentAlias: true })];
  assert.deepEqual(rangeSelection(rows, 'a', 'c'), ['a', 'b', 'c']);
  assert.deepEqual(rangeSelection(rows, 'c', 'b'), ['b', 'c']);
  assert.deepEqual(rangeSelection(rows, 'gone', 'b'), ['b']);
});

test('batch messages report partial failures with their reasons', () => {
  assert.deepEqual(batchMessage('删除', { succeeded: 2, results: [{ ok: true }, { ok: true }] }), { type: 'success', text: '已删除 2 个会话' });
  const partial = batchMessage('删除', { succeeded: 1, results: [{ ok: true }, { ok: false, error: 'conversation_running' }, { ok: false, error: 'conversation_running' }] });
  assert.deepEqual(partial, { type: 'warning', text: '已删除 1 个；2 个未删除（运行中）' });
});

test('an unset conversation retention is omitted from Webhook and Cron payload defaults', () => {
  const config = defaultConfig({ type: 'folder', id: 'F' });
  assert.deepEqual(config.target.conversationRetention, { keepLast: null, keepDays: null });
  assert.equal('conversationRetention' in submittedConfig(config).target, false);
  config.target.conversationRetention.keepLast = 20;
  assert.deepEqual(submittedConfig(config).target.conversationRetention, { keepLast: 20, keepDays: null });
  assert.equal('conversationRetention' in defaultConfig({ type: 'conversation', id: 'C' }).target, false);
  assert.deepEqual(draftOf({ config: { schedule: {}, runConfig: {} } }).config.conversationRetention, { keepLast: null, keepDays: null });
  assert.deepEqual(draftOf({ config: { conversationRetention: { keepDays: 3 } } }).config.conversationRetention, { keepLast: null, keepDays: 3 });
});
