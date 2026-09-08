import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const source = fs.readFileSync(new URL('./ConversationTree.vue', import.meta.url), 'utf8');

test('folder prompt editor is not wrapped in a native label that activates Monaco hidden IME input', () => {
  const labels = [...source.matchAll(/<label\b[^>]*>[\s\S]*?<\/label>/g)];
  assert.ok(labels.length > 0);
  assert.ok(labels.every(([label]) => !label.includes('<MdEditor')));
  assert.match(source, /class="property-field" role="group" aria-labelledby="folder-prompt-label"/);
  assert.match(source, /<span id="folder-prompt-label">/);
});

test('ordinary workspace input keeps its label and a neutral public example', () => {
  assert.match(source, /<label><span>本节点工作目录[\s\S]*?<el-input[^>]+placeholder="例如 \/home\/user\/projects\/my-project"[^>]*\/><\/label>/);
});
