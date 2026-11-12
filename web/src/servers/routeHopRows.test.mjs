import test from 'node:test';
import assert from 'node:assert/strict';
import { routeHopRows } from './serverHelpers.js';

const rows = hops => routeHopRows(hops).map(r => [r.label, r.ip || '*', r.note || '']);
// ttls that answered, of 1..last
const trace = (answered, last) => Array.from({length: last}, (_, i) => ({ttl: i + 1, ip: answered.includes(i + 1) ? `192.0.2.${i + 1}` : '', rttMs: null}));

// From 11-06: a route that never reached its target listed hops 14 to 30, each a lone "*".
test('a run of silent hops is one row, the ones after the last answer say the route goes quiet', () => {
  assert.deepEqual(rows(trace([1, 2, 3, 4, 5, 6, 8, 9, 12, 13], 30)), [
    ['1', '192.0.2.1', ''], ['2', '192.0.2.2', ''], ['3', '192.0.2.3', ''], ['4', '192.0.2.4', ''], ['5', '192.0.2.5', ''], ['6', '192.0.2.6', ''],
    ['7', '*', ''], ['8', '192.0.2.8', ''], ['9', '192.0.2.9', ''], ['10–11', '*', '2 跳没有响应'], ['12', '192.0.2.12', ''], ['13', '192.0.2.13', ''],
    ['14–30', '*', '之后 17 跳都没有响应']]);
  assert.deepEqual(rows(trace([1], 2)), [['1', '192.0.2.1', ''], ['2', '*', '']]);
  assert.deepEqual(rows(trace([], 3)), [['1–3', '*', '3 跳没有响应']]);
  assert.deepEqual(rows([]), []);
});
