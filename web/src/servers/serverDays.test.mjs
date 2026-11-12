import test from 'node:test';
import assert from 'node:assert/strict';
import { cycleStart, daysLeft, projectedTraffic, setServerZone, trafficTypeText } from './serverHelpers.js';

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

// From the 11-09 review: the projection ran to the start date's day a month on (31 January to 3 March, 28 February
// to 28 March) and from the browser's midnight; the period runs to the reset day, or the month's last, in the servers' zone.
test('the projected traffic runs to the next reset day in the servers time zone', () => {
  setServerZone('Asia/Shanghai');
  const at = date => new Date(`${date}T00:00:00+08:00`);
  let p = projectedTraffic({periodStart: '2026-01-31', resetDay: 31, used: 100}, at('2026-02-10'));
  assert.equal(p.bytes, 280);
  assert.equal(p.resetsOn.getTime(), at('2026-02-28').getTime());
  p = projectedTraffic({periodStart: '2026-02-28', resetDay: 31, used: 100}, at('2026-03-10'));
  assert.equal(p.bytes, 310);
  assert.equal(p.resetsOn.getTime(), at('2026-03-31').getTime());
  p = projectedTraffic({periodStart: '2026-12-15', resetDay: 15, used: 100}, at('2026-12-25'));
  assert.equal(p.bytes, 310);
  assert.equal(p.resetsOn.getTime(), at('2027-01-15').getTime());
  assert.equal(projectedTraffic({periodStart: '2026-02-01', resetDay: 1, used: 100}, new Date('2026-02-01T20:00:00+08:00')), null, 'less than a day in');
  // New York midnights, one hour shorter across the spring clock change.
  setServerZone('America/New_York');
  p = projectedTraffic({periodStart: '2026-03-01', resetDay: 1, used: 100}, new Date('2026-03-11T05:00:00Z'));
  assert.equal(p.resetsOn.toISOString(), '2026-04-01T04:00:00.000Z');
  assert.equal(Math.round(p.bytes * 24), Math.round(100 * (31 * 24 - 1) / 10));
  setServerZone('');
});
