import test from 'node:test';
import assert from 'node:assert/strict';
import axios from 'axios';
import { gatewayLastUpdate, gatewayUpdateHint, gatewayUpdateReasons } from './instanceHelpers.js';

test('gateway update HTTP contract: one-click update and the automatic update switch', async () => {
  const before = axios.defaults.adapter, calls = [];
  axios.defaults.adapter = async config => { calls.push({method: config.method, url: config.baseURL + config.url, data: config.data ? JSON.parse(config.data) : undefined, timeout: config.timeout}); return {data: {ok: true}, status: 200, config, headers: {}}; };
  const { Api: wire } = await import('../api.js?gateway-update-contract');
  try {
    await wire.updateInstanceGateway();
    await wire.setInstanceGatewayAutoUpdate(false);
    assert.deepEqual(calls.map(c => [c.method, c.url, c.data]), [
      ['post', '/api/instance-gateway/update', {}], ['put', '/api/instance-gateway/auto-update', {autoUpdate: false}]]);
    assert.ok(calls[0].timeout >= 240000);  // Reinstalling waits for the install script and the restart.
  } finally { axios.defaults.adapter = before; }
});

test('gateway update texts: reasons, last result and the page hint', () => {
  const now = Date.now();
  assert.equal(gatewayUpdateReasons({updateReasons: ['build_outdated', 'control_not_loopback', 'unknown']}),
    '主控带来了新版本的网关程序。更新后，网关的管理接口改为只在服务器本机监听。');
  assert.equal(gatewayUpdateReasons({updateReasons: ['build_unreported']}), '网关是旧版本，还不会报告自己的版本。');
  assert.equal(gatewayLastUpdate({}), '');
  assert.equal(gatewayLastUpdate({lastUpdate: {atMs: now, ok: true, auto: true}}, now), '上次自动更新成功（刚刚）');
  assert.equal(gatewayLastUpdate({lastUpdate: {atMs: now - 120000, ok: false, auto: false, error: 'gateway_start_failed'}}, now),
    '上次更新失败（2 分钟前）：网关已安装但服务启动失败，请查看服务器上 openbear-gateway 服务的日志。');
  // Codes without a text are shown as they are, never a generic "maybe it worked".
  assert.equal(gatewayLastUpdate({lastUpdate: {atMs: now, ok: false, auto: true, error: 'gateway_http_500'}}, now), '上次自动更新失败（刚刚）：gateway_http_500');
  assert.equal(gatewayUpdateHint({autoUpdate: true}), '主控会自动更新');
  assert.equal(gatewayUpdateHint({autoUpdate: true, lastUpdate: {ok: false, auto: true}}), '自动更新失败');
  assert.equal(gatewayUpdateHint({autoUpdate: true, lastUpdate: {ok: false, auto: false}}), '主控会自动更新');
  assert.equal(gatewayUpdateHint({autoUpdate: false, lastUpdate: {ok: false, auto: true}}), '');
});
