import test from 'node:test';
import assert from 'node:assert/strict';
import { unlockServices } from './serverHelpers.js';

const ok = region => ({state: 'ok', region, note: ''});
const failed = {state: 'error', region: '', note: 'failed'};
const titles = node => Object.fromEntries(unlockServices(node, 'DE').items.map(s => [s.id, s.label]));

// From 11-05: most dual-stack servers have an IPv6 address with no way out, and every service
// showed IPv6 检测失败 under the IPv4 verdict.
test('a failed IPv6 check is shown only on a server with nothing but IPv6', () => {
  const ips = ['198.51.100.9', '2001:db8::9'];
  const both = {info: {ipv4: ips[0], ipv6: ips[1]}, unlock: {at: 1, ips,
    v4: {google: ok('DE'), netflix: ok('DE')}, v6: {google: failed, netflix: {state: 'blocked', region: '', note: ''}}}};
  assert.deepEqual(titles(both), {google: 'Google\nIPv4：可用 · 解锁地区 德国（DE）',
    netflix: 'Netflix\nIPv4：可用 · 解锁地区 德国（DE）\nIPv6：不可用'});
  const v6only = {info: {ipv4: '', ipv6: ips[1]}, unlock: {at: 1, ips: ['', ips[1]], v6: {google: failed}}};
  const found = unlockServices(v6only, 'DE');
  assert.equal(found.family, 'v6');
  assert.deepEqual(found.items.map(s => [s.id, s.state, s.label]), [['google', 'error', 'Google\nIPv6：检测失败（连接失败）']]);
  // The IPv4 verdict itself still says when it failed.
  both.unlock.v4.google = failed;
  assert.equal(titles(both).google, 'Google\nIPv4：检测失败（连接失败）');
});
