// The existing history directory is ordered newest turn first, with a turn
// heading followed by its individual messages (latest reply first). Keep the
// headings across page boundaries. Message rows remain independent; the group
// heading retains the exact turn reference for its explicit whole-turn action.
export function conversationMessages(rows) {
  let turn = null;
  const messages = [];
  for (const item of rows) {
    if (item.kind === 'turn') { turn = item; continue; }
    if (item.kind !== 'message') continue;
    const role = item.group === '用户消息' ? 'user' : 'assistant';
    messages.push({...item, role, roleLabel: role === 'user' ? '用户输入' : '模型输出',
      groupKey: turn?.key || `chat:${item.id}`, group: turn?.label || '会话消息', turnReference: turn});
  }
  return messages;
}
