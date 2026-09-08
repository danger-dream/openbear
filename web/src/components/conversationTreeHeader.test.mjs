import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const source = fs.readFileSync(new URL('./ConversationTree.vue', import.meta.url), 'utf8');
const rule = selector => source.slice(source.indexOf(`${selector} {`)).split('}')[0];

test('tree heading and smaller count share a text baseline without changing typography', () => {
  assert.match(rule('.tree-title'), /align-items:baseline/);
  assert.match(rule('.tree-title'), /font-size:12px/);
  assert.match(rule('.tree-title'), /gap:6px/);
  assert.match(rule('.tree-title span'), /font-size:10px/);
});

test('tree heading icon remains vertically centered independently of the text baseline', () => {
  assert.match(rule('.tree-title > .el-icon'), /align-self:center/);
});
