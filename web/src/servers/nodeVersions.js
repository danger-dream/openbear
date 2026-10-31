// Where a node stands against the node build the controller picked (node.update from the
// servers listing), and how that reads in the list, the detail and the summary.

// Tags the list shows: only states that ask for something. 'ahead' (newer than the target,
// downgraded only when asked to) and 'current' stay out of the list; the detail still names them.
const STATES = {
  required: {text: '需更新', tone: 'warn', title: '缺少主控需要的功能、版本已撤回或与主控不兼容，更新后才能正常使用'},
  available: {text: '可更新', tone: '', title: '有可用的新节点版本'},
  mismatch: {text: '构建不一致', tone: '', title: '版本号相同，但运行的不是主控选定的那份程序'},
  ahead: {text: '比目标新', tone: '', title: '节点比主控选定的版本新，不会自动降级'},
};
const LIST_STATES = new Set(['required', 'available', 'mismatch']);
export const NODE_SOURCES = {local: '本地构建', release: '正式版', prerelease: '测试版', dev: '开发构建', unknown: '来源未知'};
export const TARGET_SOURCES = {local: '本地构建', github: 'GitHub'};
export const FEATURE_LABELS = {'fs.edit': '文件编辑', jobs: '后台任务', 'exec.shell': '指定 shell 执行命令', mesh: '组网', net: '网络测试'};

export function featureNames(missing) {
  return (missing || []).map(f => FEATURE_LABELS[f] || f).join('、');
}

/** The status tag of a node in the board and list; null when there is nothing to ask. */
export function nodeStateTag(node, {all = false} = {}) {
  // A controller from before update states only said whether an update was available.
  const state = node?.update ? node.update.state : node?.updateAvailable ? 'available' : '';
  if (!state || !(all ? STATES[state] : LIST_STATES.has(state))) return null;
  const tag = STATES[state];
  const missing = featureNames(node.update?.missing);
  return {state, text: tag.text, tone: tag.tone, title: state === 'required' && missing ? `更新后才能使用${missing}` : tag.title};
}

/** "abcdef123456 · 正式版": the build a node runs, for the detail and tools. */
export function nodeBuildLine(node) {
  if (!node) return '';
  const source = NODE_SOURCES[node.source] || '';
  return node.build ? [node.build, source].filter(Boolean).join(' · ') : source ? `${source} · 未上报构建` : '';
}

/** One line for the detail: what the update would do, or why the node must have it. */
export function nodeUpdateText(node) {
  const u = node?.update || {}, target = u.target || {};
  const to = target.source === 'local' ? `本地构建 ${target.version}（${target.build}）` : `${target.version}（${target.build}）`;
  if (u.state === 'required') {
    if (u.withdrawn) return `节点 ${node.version} 所跑的版本已撤回，需要${u.downgrade ? '降级' : '更新'}到 ${to}`;
    if (u.protocolOk === false) return `节点 ${node.version} 与主控的通信协议不兼容，需要更新到 ${to}`;
    const missing = featureNames(u.missing);
    return `节点 ${node.version} 需要更新${missing ? `才能使用${missing}` : ''}，可以更新到 ${to}`;
  }
  if (u.state === 'available') return `节点 ${node.version} 可以更新到${target.source === 'local' ? '' : ' '}${to}`;
  if (u.state === 'mismatch') return `节点运行的构建 ${node.build} 不是主控选定的 ${target.build}（版本同为 ${target.version}），可以更新对齐`;
  return '';
}

/** Why an online node and this controller cannot work together ('' when they can): it stays
 * connected and reporting, but is sent nothing beyond an update. */
export function incompatibleText(live) {
  const p = live?.protocol || {}, range = p.min === p.max ? `${p.max}` : `${p.min}–${p.max}`;
  if (live?.incompatible === 'node_protocol_outdated') return `节点的通信协议（版本 ${range}）比主控支持的旧，暂时不能执行命令、打开终端或参与组网；更新节点后恢复`;
  if (live?.incompatible === 'controller_protocol_outdated') return `节点的通信协议（版本 ${range}）比主控支持的新，暂时不能执行命令、打开终端或参与组网；需要先更新主控`;
  return '';
}

/** Counts behind the board's summary: online nodes that can update, those that must, and those only reinstallable. */
export function updateSummary(items) {
  const due = (items || []).filter(i => i.node?.updateAvailable);
  const direct = due.filter(i => i.node.caps?.exec !== false);
  const required = due.filter(i => i.node.update?.state === 'required');
  const missing = [...new Set(required.flatMap(i => i.node.update.missing || []))];
  return {due: due.length, direct: direct.length, required: required.length, blocked: due.length - direct.length, features: featureNames(missing)};
}

/** The message after updating one node: confirmed once it is back and reports the new build (its own word, so said as such). */
export function updateResultText(r) {
  if (!r?.changed) return {tone: 'success', text: '已经是主控选定的构建'};
  if (r.confirmed === false) return {tone: 'warning', text: '文件已替换，未确认运行：节点没有在限定时间内用新程序重新连上'};
  if (r.downgrade) return {tone: 'success', text: `已降级到 ${r.to}，节点重新连上并报告运行新构建`};
  return {tone: 'success', text: r.from === r.to ? `已更新到新构建 ${r.build || ''}，节点重新连上并报告运行新构建` : `已更新到 ${r.to}，节点重新连上并报告运行新构建`};
}

/** The message after 全部更新. */
export function updateAllText(items) {
  const list = (items || []).filter(i => i.error !== 'node_update_needs_exec');
  const failed = list.filter(i => !i.ok), unconfirmed = list.filter(i => i.ok && i.changed && i.confirmed === false);
  const updated = list.filter(i => i.ok && i.changed && i.confirmed !== false);
  if (!list.some(i => !i.ok || i.changed)) return {tone: 'success', text: '节点都已是主控选定的构建'};
  const parts = [`已更新 ${updated.length} 台`];
  if (unconfirmed.length) parts.push(`${unconfirmed.length} 台文件已替换但未确认运行`);
  if (failed.length) parts.push(`${failed.length} 台失败`);
  return {tone: failed.length || unconfirmed.length ? 'warning' : 'success', text: parts.join('，'), failed};
}
