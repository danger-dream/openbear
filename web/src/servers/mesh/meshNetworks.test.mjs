import test from 'node:test';
import assert from 'node:assert/strict';
import {lanAccess, network} from './meshNetworks.js';

test('LAN permission intersections keep family, subprefix and multiple-network scope', () => {
  const nets = ['192.168.1.0/24', '192.168.2.0/24', 'fd12:3456::/64'];
  for (const part of ['192.168.1.42/32', '192.168.1.128/25', 'fd12:3456::42/128', 'fd12:3456::/80']) {
    assert.deepEqual(lanAccess({subnets: [part]}, nets), {hit: true, part: [part]});
  }
  assert.deepEqual(lanAccess({subnets: ['192.168.1.0/24']}, nets), {hit: true, part: ['192.168.1.0/24']});
  assert.deepEqual(lanAccess({subnets: ['192.168.0.0/16', 'fd12:3456::/48']}, nets), {hit: true, part: []});
  assert.deepEqual(lanAccess({allSubnets: true}, nets), {hit: true, part: []});
  assert.deepEqual(lanAccess({subnets: ['192.168.3.0/24', 'fd12:3457::/64']}, nets), {hit: false, part: []});
  assert.deepEqual(lanAccess({prefixes: ['fd12:3456::42/128']}, nets, false), {hit: true, part: ['fd12:3456::42/128']});
  assert.deepEqual(lanAccess({subnets: ['0.0.0.0/0']}, ['::/0']), {hit: false, part: []});
});

test('IPv6 compression, embedded IPv4 and exact adjacent host boundaries', () => {
  assert.equal(network('fd12:3456::42/128').base, network('fd12:3456:0:0:0:0:0:42/128').base);
  assert.equal(network('::ffff:192.168.1.42/128').base, network('::ffff:c0a8:12a/128').base);
  assert.equal(lanAccess({subnets: ['fd12:3456::43/128']}, ['fd12:3456::42/128']).hit, false);
  for (const value of ['x/64', ':::/64', 'fd12::x/64', '::/129', '1:2:3:4:5:6:7:8:9/64', '192.168.1.1/33']) assert.equal(network(value), null);
});
