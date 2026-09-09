import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import postcss from 'postcss';

const source = fs.readFileSync(new URL('./ConversationTree.vue', import.meta.url), 'utf8');

test('folder prompt editor is not wrapped in a native label that activates Monaco hidden IME input', () => {
  const labels = [...source.matchAll(/<label\b[^>]*>[\s\S]*?<\/label>/g)];
  assert.ok(labels.length > 0);
  assert.ok(labels.every(([label]) => !label.includes('<MdEditor')));
  assert.match(source, /class="property-field" role="group" aria-labelledby="folder-prompt-label"/);
  assert.match(source, /<span id="folder-prompt-label">/);
});

test('folder properties keep readable local typography without overriding other dialogs', () => {
  const style = source.slice(source.indexOf('/* Local dialog styling:'), source.indexOf('.impact-copy {'));
  assert.match(style, /\.folder-properties-dialog \.effective-value pre \{ font-family:inherit; \}/);
  assert.match(style, /\.folder-properties-dialog \.effective-value code[^}]+font-size:13px;/);
  assert.doesNotMatch(style, /font(?:-size)?:\s*(?:10|11)px/);
  postcss.parse(style).walkRules(rule => {
    assert.ok(rule.selectors.every(selector => selector.startsWith('.folder-properties-dialog')), rule.selector);
  });
  assert.match(style, /\.folder-properties-dialog \.el-dialog__body \{[^}]*min-height:0;[^}]*overflow:auto;/);
  assert.match(style, /\.folder-properties-dialog \.el-dialog__footer \{ flex:none;/);
});

test('ordinary workspace input keeps its label and a neutral public example', () => {
  assert.match(source, /<label\b[^>]*>\s*<span id="folder-workspace-label">本节点工作目录[\s\S]*?<el-input[^>]+placeholder="例如 \/home\/user\/projects\/my-project"[^>]*\/>\s*<\/label>/);
});
