import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const source = fs.readFileSync(new URL('./ConversationTree.vue', import.meta.url), 'utf8');

test('folder prompt editor is not wrapped in a native label that activates Monaco hidden IME input', () => {
  const labels = [...source.matchAll(/<label\b[^>]*>[\s\S]*?<\/label>/g)];
  assert.ok(labels.length > 0);
  assert.ok(labels.every(([label]) => !label.includes('<MdEditor') && !label.includes('<AdaptiveMdEditor')));
  assert.match(source, /<PromptPolicyEditor v-model="folderPromptPolicy"/);
  const policy = fs.readFileSync(new URL('./PromptPolicyEditor.vue', import.meta.url), 'utf8');
  assert.doesNotMatch(policy, /<label\b/);
  assert.match(policy, /class="folder-prompt-editor"/);
});
