import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {errorText, safeError} from './serverHelpers.js';

const added = ['check_not_for_front', 'invalid_front', 'invalid_front_address', 'invalid_front_exit', 'front_exit_duplicate',
  'front_exit_is_front', 'front_is_exit', 'front_exit_not_ready', 'front_exit_offline', 'front_serve_failed', 'front_port_fronts_offline',
  'invalid_front_port', 'invalid_httpbl_key', 'secret_save_failed', 'no_public_ipv4', 'invalid_ipcheck_days', 'invalid_unit',
  'node_update_running', 'command_required', 'pattern_timeout'];

test('new server errors explain the reason in Chinese instead of echoing a code', () => {
  for (const code of added) {
    const text = errorText(code);
    assert.match(text, /[\u4e00-\u9fff]/, code);
    assert.ok(!text.includes(code), code);
    for (const status of [400, 409, 500]) assert.equal(safeError({response: {status, data: {error: code}}}), text);
  }
  assert.match(errorText('front_port_fronts_offline'), /所有前置机上线.*修改.*端口/);
  assert.match(errorText('invalid_httpbl_key'), /12 位小写字母/);
  assert.match(errorText('front_is_exit'), /更换出口/);
});

test('literal errors of the four reviewed server services have a Chinese mapping (including instance fallback)', () => {
  for (const file of ['front.py', 'ip_intel.py', 'checks.py', 'service.py']) {
    const source = readFileSync(new URL(`../../../app/servers/${file}`, import.meta.url), 'utf8');
    const codes = new Set([...source.matchAll(/InstanceError\('([a-z0-9_]+)'/g)].map(m => m[1]));
    // checks.py constructs these from its KEY rather than using a literal.
    if (file === 'checks.py') for (const code of ['unlock_check_disabled', 'route_check_disabled']) codes.add(code);
    for (const code of codes) assert.ok(!errorText(code).includes(code), `${file}: ${code}`);
  }
});
