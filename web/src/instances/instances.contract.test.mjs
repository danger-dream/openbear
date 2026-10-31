import test from 'node:test';
import assert from 'node:assert/strict';
import axios from 'axios';
import { canManageInstances } from '../authMode.js';
import { injectedMode } from '../sessionBoundary.js';
import { childClient, defaultAccessUrl, displayStatus, errorText, generatePassword, needsMapping, passwordHint, publicLink, relativeTime, safeError, serverChannel, serverLabel, serverUnavailable, serverUse, statusLabel, withServerPage } from './instanceHelpers.js';

// Exercise the production axios client with a transport adapter (not a parallel
// reimplementation of URLs). The adapter sees exactly the outgoing wire payload.
test('controller HTTP contract uses exact methods, paths and payloads', async () => {
  const before = axios.defaults.adapter, calls = [];
  axios.defaults.adapter = async config => { calls.push({method: config.method, url: config.baseURL + config.url, params: config.params, data: config.data ? JSON.parse(config.data) : undefined}); return {data: {ok: true}, status: 200, config, headers: {}}; };
  const { Api: wire } = await import('../api.js?instance-contract');
  try {
    await wire.instanceGateway();
    await wire.enableInstanceGateway({serverId: 'local', port: 18962, publicUrl: 'http://203.0.113.10:18962'});
    await wire.setInstanceGatewayUrl('https://bear.example.com');
    await wire.setInstanceGatewayProxies(['192.0.2.10']);
    await wire.disableInstanceGateway();
    await wire.checkInstanceGatewayPort('srv/1', 18962);
    await wire.instanceServers(); await wire.createInstanceServer({name: 's'}); await wire.updateInstanceServer('srv/1', {name: 't'});
    await wire.checkInstanceServer('srv/1'); await wire.deleteInstanceServer('srv/1'); await wire.instanceSshKeys();
    await wire.createInstance({name: 'child'}); await wire.updateInstance('child/1', {account: {password: 'x'}});
    await wire.maintainInstance('child/1', {instructions: 'repair only'}); await wire.syncInstanceGateway('child/1');
    await wire.instanceRequest('child/1', {method: 'PATCH', path: '/api/settings/agent.maxTurns', body: {value: 30}});
    await wire.rotateInstanceGatewayKeys(); await wire.rotateInstanceKeys('child/1'); await wire.migrateInstanceNetwork('child/1');
    assert.deepEqual(calls.map(c => [c.method, c.url]), [
      ['get', '/api/instance-gateway'], ['post', '/api/instance-gateway/enable'], ['put', '/api/instance-gateway/public-url'],
      ['put', '/api/instance-gateway/client-ip'], ['post', '/api/instance-gateway/disable'], ['get', '/api/instance-gateway/port'],
      ['get', '/api/instance-servers'], ['post', '/api/instance-servers'], ['patch', '/api/instance-servers/srv%2F1'],
      ['post', '/api/instance-servers/srv%2F1/check'], ['delete', '/api/instance-servers/srv%2F1'], ['get', '/api/instance-servers/ssh-keys'],
      ['post', '/api/instances'], ['patch', '/api/instances/child%2F1'], ['post', '/api/instances/child%2F1/maintenance'],
      ['post', '/api/instances/child%2F1/gateway-sync'], ['post', '/api/instances/child%2F1/request'],
      ['post', '/api/instance-gateway/rotate-keys'], ['post', '/api/instances/child%2F1/rotate-keys'], ['post', '/api/instances/child%2F1/migrate-network'],
    ]);
    assert.deepEqual(calls[1].data, {serverId: 'local', port: 18962, publicUrl: 'http://203.0.113.10:18962'});
    assert.deepEqual(calls[2].data, {publicUrl: 'https://bear.example.com'});
    assert.deepEqual(calls[3].data, {trustedProxies: ['192.0.2.10']});
    assert.deepEqual(calls[5].params, {serverId: 'srv/1', port: 18962});
    assert.ok(!calls.some(c => c.url === '/api/auth/mode'));
  } finally { axios.defaults.adapter = before; }
});
test('login mode comes from the page; an untouched marker is the controller', () => {
  for (const mode of ['controller', 'managed', 'gateway']) assert.equal(injectedMode({__OPENBEAR_AUTH_MODE__: mode}), mode);
  for (const value of ['[[controller]]', undefined, 'other', '']) assert.equal(injectedMode({__OPENBEAR_AUTH_MODE__: value}), 'controller');
  assert.equal(canManageInstances('controller'), true);
  for (const role of ['managed', 'gateway', null, undefined]) assert.equal(canManageInstances(role), false);
});
test('fixed child relay handles nested HTTP failures without leaking server text or falling back to parent', async () => {
  const calls = [], relay = childClient('fixed-target', {instanceRequest: async (...args) => { calls.push(args); return {ok:true,status:200,data:{ok:true,items:[1]}}; }});
  assert.deepEqual(await relay('GET','/api/memory/entries'), {ok:true,items:[1]});
  await relay('PATCH','/api/channels/p/models/model-id', {name:'model'});
  assert.ok(calls.every(([id]) => id === 'fixed-target'));
  for (const path of ['/api/instances','https://evil.test/api/settings','//evil.test/api/settings','/api/auth/password/login','/api/system/update','/api/managed/v1/config/../status']) await assert.rejects(relay('GET',path));
  for (const status of [400,401,403,500]) {
    const bad = childClient('child', {instanceRequest:async () => ({ok:true,status,data:{ok:false,error:'secret-value'}})});
    await assert.rejects(bad('GET','/api/settings'), e => !safeError(e).includes('secret-value'));
  }
  assert.ok(!safeError({response:{data:{error:'password-secret'}}}).includes('password-secret'));
});
test('labels, errors, generated passwords and address helpers', () => {
  assert.equal(displayStatus({status:'ready',healthStatus:'degraded'}), 'needs_verification');
  assert.equal(displayStatus({status:'ready',healthStatus:'unreachable'}), 'unreachable');
  assert.equal(statusLabel('ready'), '正常'); assert.equal(statusLabel('unreachable'), '无法连接'); assert.equal(statusLabel('needs_verification'), '待检查');
  assert.equal(errorText('gateway_port_in_use'), '这个端口已被占用，请换一个。');
  assert.equal(errorText('gateway_install_needs_root'), '安装网关需要 root 权限：请使用 root 登录，或为该用户配置免密 sudo。');
  assert.equal(publicLink('javascript:alert(1)'), ''); assert.equal(publicLink('https://user:pass@example.com'), '');
  const passwords = new Set(Array.from({length: 50}, () => generatePassword()));
  assert.equal(passwords.size, 50);
  for (const value of passwords) assert.match(value, /^[a-zA-Z2-9]{4}-[a-zA-Z2-9]{4}-[a-zA-Z2-9]{4}$/);
  assert.equal(defaultAccessUrl('203.0.113.10', 18962), 'http://203.0.113.10:18962');
  assert.equal(needsMapping('http://203.0.113.10:18962', '203.0.113.10', 18962), false);
  assert.equal(needsMapping('https://bear.example.com', '203.0.113.10', 18962), true);
  assert.equal(needsMapping('http://203.0.113.10:80', '203.0.113.10', 18962), true);
  assert.equal(serverLabel({local: true}), '本机（主控所在服务器）');
  assert.equal(serverLabel({name: '东京', host: '192.0.2.12', status: 'unreachable'}), '东京 · 192.0.2.12（无法连接）');
  // An online node with command execution carries instance commands, SSH or not.
  assert.equal(serverLabel({name: '东京', host: '192.0.2.12', auth: 'password', node: {online: true, exec: true}}), '东京 · 节点通道');
  assert.equal(serverLabel({name: '东京', host: '192.0.2.12', auth: 'password', node: {online: false, exec: true}}), '东京 · 192.0.2.12');
  assert.equal(serverLabel({name: '大阪', host: '', auth: 'none', node: {online: false, exec: true}}), '大阪 · 节点离线');
  assert.equal(serverLabel({name: '大阪', host: '', auth: 'none', node: {online: true, exec: false}}), '大阪 · 节点未开启命令执行');
  assert.deepEqual(serverUse({usedBy: ['访问网关', '实例甲', '实例乙']}), {gateway: true, instances: ['实例甲', '实例乙']});
  assert.equal(serverUnavailable({auth: 'none', node: {online: false, exec: true}}), '节点离线');
  assert.equal(serverUnavailable({auth: 'password', node: {online: false, exec: true}}), '');
  assert.equal(serverChannel({auth: 'password', status: 'unreachable'}), 'SSH 无法连接');
  assert.equal(serverChannel({auth: 'password', node: {online: true, exec: true}}), '节点');
  // The servers page supplies the host's own name, regions and the order; unknown servers go last.
  assert.deepEqual(withServerPage([{id: 'local', name: '本机', local: true}, {id: 'b'}, {id: 'x'}], [{id: 'b', name: 'B', region: 'JP'}, {id: 'local', name: 'Edge-SG', region: 'SG'}])
    .map(s => [s.id, s.name, s.region]), [['b', 'B', 'JP'], ['local', 'Edge-SG', 'SG'], ['x', undefined, undefined]]);
  assert.equal(relativeTime(1000, 1000 + 5 * 60000), '5 分钟前');
  // Advice only, never a block.
  assert.equal(passwordHint(''), '');
  assert.match(passwordHint('wabac'), /较短/);
  assert.match(passwordHint('wabacwabac'), /建议 12 位以上/);
  assert.equal(passwordHint('wabac?123'), '');
  assert.equal(passwordHint(generatePassword()), '');
});
