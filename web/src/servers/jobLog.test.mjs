import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { FOLLOW_GAP, LOG_LIMIT, followDelay, keepRecent } from './jobLog.js';

test('the job log keeps only its newest part, from a line start, and says it was cut', () => {
  const small = {data: 'a\nb\n', start: 10};
  assert.equal(keepRecent(small), small, 'within the limit nothing changes');
  const lines = Array.from({length: 40}, (_, i) => `第${i}行`).join('\n') + '\n';
  const kept = keepRecent({data: lines, start: 100}, 50);
  assert.ok(kept.trimmed && kept.data.length <= 50 && lines.endsWith(kept.data));
  assert.match(kept.data, /^第\d+行\n/, 'starts at a whole line');
  assert.equal(kept.start, 100 + new TextEncoder().encode(lines.slice(0, lines.length - kept.data.length)).length, 'start stays a byte offset');
  const noLines = keepRecent({data: 'x'.repeat(LOG_LIMIT + 10_000), start: 0});
  assert.equal(noLines.data.length, LOG_LIMIT);
  let log = {data: '', start: 0};
  for (let i = 0; i < 40; i++) log = keepRecent({...log, data: log.data + `${i}`.padEnd(64 * 1024, '.') + '\n'});
  assert.ok(log.data.length <= LOG_LIMIT && log.data.endsWith('\n') && log.data.includes('39'));
});

test('reading on waits at least FOLLOW_GAP, and a reply that does not move forward is not chased', () => {
  assert.equal(followDelay({next: 100, size: 500}, 50), FOLLOW_GAP);
  assert.equal(followDelay({next: 100, size: 500}, undefined), FOLLOW_GAP);
  assert.equal(followDelay({next: 100, size: 500}, 100), 2000, 'no progress');
  assert.equal(followDelay({next: 500, size: 500}, 100), 2000, 'caught up');
  assert.ok(FOLLOW_GAP >= 300);
  const view = fs.readFileSync(new URL('./ServerJobs.vue', import.meta.url), 'utf8');
  assert.doesNotMatch(view, /setTimeout\(follow, [^)]*\? 0 :/);
  assert.match(view, /const delay = followDelay\(part, current\?\.next\);\n\s*if \(data\.job\.state === 'running' \|\| delay === FOLLOW_GAP\) logTimer = setTimeout\(follow, delay\);/);
  assert.equal((view.match(/keepRecent\(/g) || []).length, 2, 'both the first tail and every append are capped');
  assert.match(view, /<p v-if="log\?\.trimmed"[^>]*>输出较多，这里只保留最近约/);
});
