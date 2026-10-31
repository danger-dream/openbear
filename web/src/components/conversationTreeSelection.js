// Multi-select is keyed by conversationUuid so recent, search and folder rows of
// one conversation share a single selection.
export const AUTOMATIC_ORIGINS = new Set(['webhook', 'cron']);

const BATCH_ERRORS = {conversation_running: '运行中', conversation_delete_busy: '正忙', conversation_not_found: '已不存在'};

export function selectableRow(row) {
  return row?.kind === 'conversation' && !row.local && Boolean(row.conversationUuid);
}

export function automaticRow(row) {
  return AUTOMATIC_ORIGINS.has(String(row?.origin || row?.originKind || row?.origin_kind || ''));
}

export function rangeSelection(rows, fromUuid, toUuid) {
  const ids = [...new Set(rows.filter(selectableRow).map(row => row.conversationUuid))];
  const from = ids.indexOf(fromUuid), to = ids.indexOf(toUuid);
  if (from < 0 || to < 0) return [toUuid];
  return ids.slice(Math.min(from, to), Math.max(from, to) + 1);
}

export function batchMessage(verb, result = {}) {
  const failed = (result.results || []).filter(item => !item.ok);
  if (!failed.length) return {type: 'success', text: `已${verb} ${result.succeeded || 0} 个会话`};
  const reasons = [...new Set(failed.map(item => BATCH_ERRORS[item.error] || item.message || item.error))];
  return {type: 'warning', text: `已${verb} ${result.succeeded || 0} 个；${failed.length} 个未${verb}（${reasons.join('、')}）`};
}
