// 服务解锁's list: every service the full check can name has an icon (inside the sprite when a cell), the
// cards' verdicts take the place of the full check's for their nine unless they could not tell, and without
// a full result the nine alone, grouped and counted.
import test from 'node:test';
import assert from 'node:assert/strict';
import {registerVueLoader} from '../../testing/vueLoader.mjs';
import FULL from '../testing/unlockFullSample.json' with {type: 'json'};
import IDS from '../testing/unlockServiceIds.json' with {type: 'json'};

registerVueLoader();
const {SPRITE, serviceLogo} = await import('../serviceLogos.js');
const {unlockList} = await import('./unlockList.js');
const {unlockServices} = await import('../serverHelpers.js');

const nine = (states = {}) => unlockServices({info: {ipv4: '198.51.100.9'}, unlock: {at: 1, ips: ['198.51.100.9', ''], v4: Object.fromEntries(
  ['google', 'youtube', 'openai', 'anthropic', 'gemini', 'netflix', 'spotify', 'reddit', 'tiktok'].map(k => [k, {state: 'ok', region: 'HK', note: '', ...states[k]}]))}}, 'HK');

test('every service the full check can name has its icon', () => {
  assert.equal(IDS.ids.length, 156);
  for (const id of IDS.ids) {
    const logo = serviceLogo(id);
    assert.ok(logo, `${id} has an icon`);
    if (logo.svg) assert.ok(logo.svg.path && /^#[0-9A-F]{6}$/i.test(logo.svg.color), id);
    else assert.ok(logo.cell.x < SPRITE.cols && logo.cell.y < SPRITE.rows && ['', 'ink', 'paper'].includes(logo.cell.tone), id);
  }
  // The same service in other regions shares one.
  assert.deepEqual(serviceLogo('MediaUnlockTest_SKY_DE'), serviceLogo('MediaUnlockTest_SkyGo'));
  assert.deepEqual(serviceLogo('MediaUnlockTest_BGlobalVN'), serviceLogo('MediaUnlockTest_BilibiliTW'));
  // The cards' nine are the cards' own; one the full check adds later has none, and shows its initial.
  assert.equal(serviceLogo('netflix').svg.path.slice(0, 12), 'm5.398 0 8.3');
  assert.equal(serviceLogo('MediaUnlockTest_SomethingNew'), null);
  assert.equal(SPRITE.src, './serviceLogos.webp');
});

test('the full list with the cards\' verdicts in their place', () => {
  const card = nine({netflix: {state: 'limited', note: 'originals'}, reddit: {state: 'blocked', note: 'datacenter'}, anthropic: {state: 'error', region: '', note: 'challenge'}});
  const {items, counts, groups} = unlockList(FULL, card);
  assert.equal(items.length, 58);
  assert.deepEqual(groups.map(g => [g.key, g.name, g.items.length]), [['media', '流媒体', 39], ['ai', 'AI', 8], ['web', '网站与游戏', 11]]);
  const by = Object.fromEntries(items.map(i => [i.id, i]));
  assert.deepEqual([by.netflix.state, by.netflix.text, by.netflix.notes], ['limited', '受限', ['仅自制剧']]);
  assert.match(by.netflix.label, /^Netflix\nIPv4：部分可用（仅自制剧）/);
  assert.deepEqual([by.reddit.state, by.reddit.text], ['blocked', '不可用']);
  // The cards could not tell for Anthropic: the full check's verdict stands.
  assert.deepEqual([by.anthropic.state, by.anthropic.region, by.anthropic.text], ['ok', 'ES', 'ES']);
  assert.equal(by.anthropic.label, 'Anthropic\n可用 · 西班牙（ES）');
  // The full check's own note stays with the cards' verdict.
  assert.deepEqual([by.spotify.region, by.spotify.notes], ['HK', ['不能注册新账号']]);
  assert.equal(by.MediaUnlockTest_Dazn.region, 'ES');
  assert.deepEqual(counts, {ok: 30, limited: 1, blocked: 13, error: 14});
  assert.equal(Object.values(counts).reduce((a, b) => a + b), 58);
});

test('without a full result, the nine alone', () => {
  const {items, groups, counts} = unlockList(null, nine({youtube: {state: 'blocked'}}));
  assert.deepEqual(groups.map(g => [g.key, g.items.map(i => i.id)]),
    [['media', ['youtube', 'netflix', 'spotify', 'tiktok']], ['ai', ['openai', 'anthropic', 'gemini']], ['web', ['google', 'reddit']]]);
  assert.deepEqual(counts, {ok: 8, limited: 0, blocked: 1, error: 0});
  assert.ok(items.every(i => i.logo?.svg));
  assert.deepEqual(unlockList(null, null), {items: [], counts: {ok: 0, limited: 0, blocked: 0, error: 0}, groups: []});
});
