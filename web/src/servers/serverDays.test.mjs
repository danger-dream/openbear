import test from 'node:test';
import assert from 'node:assert/strict';
import { cycleStart, daysLeft, setServerZone, trafficTypeText } from './serverHelpers.js';

// From the 10-30 check: the days left counted from the browser's date, the reminders from the
// servers' time zone, so the card and the Telegram reminder could say different days.
test('days left count from today in the servers time zone', () => {
  const now = new Date('2026-10-30T20:00:00Z'); // 10-31 04:00 in Shanghai, 10-30 in New York
  setServerZone('Asia/Shanghai');
  assert.equal(daysLeft('2026-10-31', now), 0);
  assert.equal(daysLeft('2026-11-30', now), 30);
  setServerZone('America/New_York');
  assert.equal(daysLeft('2026-10-31', now), 1);
  setServerZone('Not/AZone');
  assert.equal(typeof daysLeft('2026-10-31', now), 'number');
  setServerZone('');
});

// A start date left empty: one cycle before the expiry, as the controller fills it in.
test('the start of a billing cycle from its expiry', () => {
  assert.equal(cycleStart('2026-03-31', 30), '2026-02-28');
  assert.equal(cycleStart('2027-04-07', 365), '2026-04-07');
  assert.equal(cycleStart('2026-05-31', 90), '2026-02-28');
  assert.equal(cycleStart('2026-01-15', 30), '2025-12-15');
  assert.equal(cycleStart('2026-03-05', 10), '2026-02-23');
  assert.equal(cycleStart('2026-03-31', -1), '');
  assert.equal(cycleStart('', 30), '');
});

test('traffic counting in words; a profile without a type counts the larger direction', () => {
  assert.deepEqual(['sum', 'up', 'down', 'max', 'min', undefined].map(trafficTypeText), ['双向计费', '单向：上行', '单向：下行', '取大（上下行较大的一方）', '取小（上下行较小的一方）', '取大（上下行较大的一方）']);
});
