import test from 'node:test';
import assert from 'node:assert/strict';
import {statusLabel} from './webhookConfig.js';

test('pause reasons read as what holds the endpoint, not as internal keys', () => {
  assert.equal(statusLabel('review:771f5c58-82f3-4d7f-9df1-262c8c1c5473'), '有事件结果未知，等待核实');
  assert.equal(statusLabel('retry'), '模型调用失败，稍后自动重试');
  assert.equal(statusLabel('needs_review'), '待核实');
  assert.equal(statusLabel('receiving'), '接收可用');
});
