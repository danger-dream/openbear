import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('./ConversationTree.vue', import.meta.url), 'utf8');
const start = source.indexOf('async function renameRow(row) {');
const end = source.indexOf('async function togglePin(row) {', start);
assert.ok(start >= 0 && end > start);

test('manual conversation rename accepts and sends a long title without truncation', async () => {
  const title = 'zcode parrot claude ' + '详细命名'.repeat(100);
  const updates = [];
  let validate;
  const context = vm.createContext({
    rowLabel: row => row.title,
    ElMessageBox: { prompt: async (_message, _heading, options) => {
      validate = options.inputValidator;
      return {value: `  ${title}  `};
    } },
    Api: {updateConversation: async (uuid, payload) => {
      updates.push({uuid, title: payload.title});
      return {conversation: {title: payload.title}};
    }},
    applyMutationRow: (row, updated) => { row.title = updated.title; },
    query: {value: ''},
    ElMessage: {error: error => { throw new Error(error); }},
    apiError: String,
  });
  vm.runInContext(source.slice(start, end), context);
  const row = {kind: 'conversation', conversationUuid: 'long-name', title: '原名称'};
  await context.renameRow(row);
  assert.equal(validate(title), true);
  assert.deepEqual(updates, [{uuid: 'long-name', title}]);
  assert.equal(row.title, title);
});
