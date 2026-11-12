export function stopWorkConfirmation(online) {
  return '节点上正在运行的任务和评测会被强制停止。' + (online ? '' : '节点离线，无法停止。');
}

export function stopWorkWarning(work) {
  if (!work) return '';
  const warnings = [];
  if (work.offline) warnings.push('节点离线，没能停止它上面仍在运行的任务');
  for (const item of work.failed || []) warnings.push(`${item.label || item.id}：${item.error === 'timeout' ? '停止超时' : item.error}`);
  if (work.requested?.length) warnings.push(`已请求停止 ${work.requested.length} 项评测，尚未确认结束`);
  return warnings.join('；');
}

export function stopWorkFailure(error) {
  const data = error?.response?.data;
  if (data?.error !== 'server_work_stop_failed') return '';
  return '任务或评测尚未确认停止，未删除或卸载，管理信息已保留。' + stopWorkWarning({failed: data.errors || []});
}
