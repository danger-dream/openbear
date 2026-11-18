import test from 'node:test';
import assert from 'node:assert/strict';
import {retryDelay, canStopJob} from './jobLog.js';
import {pointerButtonMask} from './desktopInput.js';

test('transient retry has bounded exponential delay and a finite budget', () => {
  const error = {response: {status: 502}};
  assert.deepEqual([1, 2, 3, 4, 5, 6, 20].map(n => retryDelay(error, n)), [1000, 2000, 4000, 8000, 16000, null, null]);
  for (const code of ['node_offline', 'node_connection_lost', 'node_busy']) {
    assert.equal(retryDelay({response: {status: 409, data: {error: code}}}, 1), 1000);
  }
  for (const code of ['job_not_found', 'job_unreadable', 'invalid_offset']) {
    assert.equal(retryDelay({response: {status: 502, data: {error: code}}}, 1), null);
  }
  assert.equal(retryDelay({response: {status: 403}}, 1), null);
});

test('interrupted jobs require positive backend evidence, legacy running remains stoppable', () => {
  assert.equal(canStopJob({state: 'running'}), true);
  for (const canStop of [undefined, null, false, 'true', 1]) assert.equal(canStopJob({state: 'interrupted', canStop}), false);
  assert.equal(canStopJob({state: 'interrupted', canStop: true}), true);
  assert.equal(canStopJob({state: 'running', canStop: false}), false);
});

test('VNC mask represents all actual held buttons, not a gesture flag', () => {
  const held = new Set([0, 2]);
  assert.equal(pointerButtonMask(held), 5);
  held.delete(2);
  assert.equal(pointerButtonMask(held), 1);
  held.add(1);
  assert.equal(pointerButtonMask(held), 3);
  held.clear();
  assert.equal(pointerButtonMask(held), 0);
});
