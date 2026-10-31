// UI editing contract. The server remains authoritative for permission, CAS and limits.
export const TRIGGER_TABS = [
  ['connection', '连接'], ['processing', '处理指令'], ['scripts', '脚本'],
  ['batching', '收集与排队'], ['statistics', '统计'], ['advanced', '高级'],
];
export const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
export function scriptDefaults(post = false) {
  return {enabled: false, runtime: 'python', code: '', timeoutSeconds: null, retry: {maxAttempts: 1, backoffSeconds: [2, 10, 30, 60], idempotencyDeclaration: null},
    ...(post ? {onOutcomes: ['normal']} : {onError: 'hold'})};
}
export function defaultConfig(scope = {}) {
  return {
    target: {mode: scope.type === 'conversation' ? 'fixedConversation' : 'newConversation', conversationId: scope.type === 'conversation' ? scope.id : null, runConfig: scope.type === 'conversation' ? null : {mainModel:null,mainThinkingLevel:null,mainFastMode:null}, ...(scope.type === 'conversation' ? {} : {conversationRetention: {keepLast: null, keepDays: null}})},
    processing: {instructions: '', eventTemplate: null, resultSchema: null, scriptScheduling: 'independent', allowServerChanges: false},
    pre: scriptDefaults(), post: scriptDefaults(true),
    idempotency: {bodyIdPath: null}, correlation: {idPath: null, originPath: null, ignoreOwnEcho: false, ownOriginValues: []},
    batching: {enabled: true, idleSeconds: 3, maxWaitSeconds: 30, maxEvents: 100, maxBytes: 65536, oversizeEvent: 'hold'},
    expiry: {ttlSeconds: null, basis: 'receivedAt', timestampPath: null, sourceTimeFormat: 'rfc3339', missingTimestamp: 'hold', maxFutureSkewSeconds: 300, onExpired: 'expire'},
    limits: {requestsPerMinute: null, pendingEvents: null, pendingBytes: null, preConcurrency: null, postConcurrency: null, autoModelConcurrency: null},
    statistics: {dimensions: [], metricDefinitions: [], businessFields: []}, notifications: {policy: 'inherit', digestSeconds: null},
  };
}
export function mergeConfig(base, value) {
  const result = clone(base);
  for (const [key, item] of Object.entries(value || {})) {
    if (!Object.hasOwn(result,key)) continue;
    result[key] = item && typeof item === 'object' && !Array.isArray(item) && result[key] && typeof result[key] === 'object'
      ? mergeConfig(result[key], item) : clone(item);
  }
  return result;
}
// An unset retention is omitted so the payload stays valid for a backend without the field.
export function submittedConfig(config) {
  const result = clone(config);
  if (result.processing?.allowServerChanges === false) delete result.processing.allowServerChanges;
  const retention = result.target?.conversationRetention;
  if (retention && retention.keepLast == null && retention.keepDays == null) delete result.target.conversationRetention;
  return result;
}
export function getPath(object, path) { return path.split('.').reduce((value, key) => value?.[key], object); }
export function setPath(object, path, value) {
  const keys = path.split('.'); let target = object;
  for (const key of keys.slice(0, -1)) target = target[key] ??= {};
  target[keys.at(-1)] = value;
}
export const BYTES_PER_MB = 1024 * 1024;
export const bytesToMb = value => value == null ? null : value / BYTES_PER_MB;
export const mbToBytes = value => value === '' || value == null ? null : Math.round(Number(value) * BYTES_PER_MB);
export function nullableNumber(value) { return value === '' || value == null ? null : Number(value); }
export function parseLines(value) { return String(value || '').split(/[\n,]/u).map(x => x.trim()).filter(Boolean); }
export function serializableDraft(draft) { return JSON.stringify({name: draft.name, description: draft.description, enabled: draft.enabled, config: draft.config}); }
// Environment is the API's nested system configuration, not a second limits contract.
export function endpointLimit(environment, config, field) {
  const system = environment.limits || {};
  const paths = {
    requestsPerMinute: ['ingress.requestsPerMinute', 'ingress.requestsPerMinute'],
    pendingEvents: ['queue.maxEvents', 'queue.maxEvents'],
    pendingBytes: ['queue.maxBytes', 'queue.maxBytes'],
    preConcurrency: ['concurrency.preScripts', 'concurrency.preScripts'],
    postConcurrency: ['concurrency.postScripts', 'concurrency.postScripts'],
    autoModelConcurrency: ['concurrency.autoModelRuns', 'concurrency.autoModelRuns'],
  };
  const [defaultPath, maxPath] = paths[field] || [];
  const defaultValue = defaultPath ? getPath(system, defaultPath) : null;
  const maximum = maxPath ? getPath(system, maxPath) : null;
  const value = config.limits[field];
  const chosen = value ?? defaultValue;
  const fixed = field === 'autoModelConcurrency' && config.target.mode === 'fixedConversation';
  const effective = chosen == null ? null : Math.min(chosen, maximum ?? Infinity, fixed ? 1 : Infinity);
  return {defaultValue, maximum, effective, source: value == null ? '继承系统限制' : '此入口单独限制', fixed};
}
export function configErrors(config, {enabled = false, environment = {}, scope = {}} = {}) {
  const errors = [];
  const add = (path, message, tab) => errors.push({path, message, tab});
  const numeric = (path, {nullable = false, zero = false, integer = false, max, tab = 'batching'} = {}) => {
    const value = getPath(config, path);
    if (nullable && value === null) return;
    if (typeof value !== 'number' || !Number.isFinite(value) || (zero ? value < 0 : value <= 0) || (integer && !Number.isInteger(value))) add(path, path.endsWith('Bytes') ? `请填写大于 0 的 MB 数值，可用小数${nullable ? '，或留空继承系统' : ''}` : `请填写${zero ? '非负' : '正'}${integer ? '整数' : '数'}${nullable ? '，或留空' : ''}`, tab);
    else if (max != null && value > max) add(path, path.endsWith('Bytes') ? `填写值 ${bytesToMb(value)} MB 超过系统上限 ${bytesToMb(max)} MB` : `填写值 ${value} 超过当前系统上限 ${max}，请调整后保存`, tab);
  };
  if (scope.type === 'conversation' && (config.target.mode !== 'fixedConversation' || config.target.conversationId !== scope.id)) add('target.conversationId', '会话入口只能指向当前会话', 'connection');
  if (config.target.mode === 'fixedConversation' && !config.target.conversationId) add('target.conversationId', '请选择具体的目标会话', 'connection');
  if (config.target.mode === 'newConversation' && config.target.conversationId != null) add('target.conversationId', '新会话模式不能指定已有会话', 'connection');
  if (config.target.mode === 'newConversation' && config.target.conversationRetention) {
    numeric('target.conversationRetention.keepLast', {nullable: true, integer: true, max: 10000, tab: 'connection'});
    numeric('target.conversationRetention.keepDays', {nullable: true, integer: true, max: 3650, tab: 'connection'});
  }
  if (enabled && !config.processing.instructions.trim()) add('processing.instructions', '启用入口前请填写处理目标、范围与授权', 'processing');
  for (const phase of ['pre', 'post']) {
    const script = config[phase];
    if (!script.enabled) continue;
    if (!script.code.trim()) add(`${phase}.code`, '启用脚本前请填写代码', 'scripts');
    const runtime = environment.runtimes?.find(item => item.runtime === script.runtime);
    if (runtime && !runtime.available) add(`${phase}.runtime`, '服务器没有可用的运行环境；可保留代码但不能启用', 'scripts');
    numeric(`${phase}.timeoutSeconds`, {nullable: true, max: environment.limits?.scripts?.maxTimeoutSeconds, tab: 'scripts'});
    numeric(`${phase}.retry.maxAttempts`, {integer: true, max: 5, tab: 'scripts'});
    if (script.retry.maxAttempts > 1 && !String(script.retry.idempotencyDeclaration || '').trim()) add(`${phase}.retry.idempotencyDeclaration`, '自动重试需说明稳定业务标识和下游防重／结果查询方式', 'scripts');
    if (phase === 'post' && !script.onOutcomes.length) add('post.onOutcomes', '至少选择一种后置触发结果', 'scripts');
  }
  numeric('batching.idleSeconds', {nullable: true}); numeric('batching.maxWaitSeconds', {nullable: true});
  numeric('batching.maxEvents', {integer: true, max: environment.limits?.batching?.maxBatchEvents});
  numeric('batching.maxBytes', {integer: true, max: environment.limits?.batching?.maxBatchBytes});
  // OR conditions deliberately allow maxWaitSeconds < idleSeconds.
  numeric('expiry.ttlSeconds', {nullable: true}); numeric('expiry.maxFutureSkewSeconds', {zero: true});
  if (config.expiry.ttlSeconds != null && config.expiry.basis === 'sourceField' && !config.expiry.timestampPath) add('expiry.timestampPath', '按事件时间计算需填写时间字段路径', 'batching');
  for (const field of Object.keys(config.limits)) numeric(`limits.${field}`, {nullable: true, integer: true, max: endpointLimit(environment, config, field).maximum, tab:'advanced'});
  numeric('notifications.digestSeconds', {nullable:true, integer:true, max:3600, tab:'advanced'});
  if (config.correlation.ignoreOwnEcho && (!config.correlation.originPath || !config.correlation.ownOriginValues.length)) add('correlation.originPath', '忽略自己的回调需要填写来源字段和自己的标识', 'advanced');
  const names = new Set();
  for (const [index, dimension] of config.statistics.dimensions.entries()) {
    if (!dimension.name?.trim() || !dimension.path?.trim() || names.has(dimension.name)) add(`statistics.dimensions.${index}`, '统计维度需唯一名称和取值路径', 'statistics');
    names.add(dimension.name);
  }
  const metrics = new Set();
  for (const [index, metric] of config.statistics.metricDefinitions.entries()) {
    const path = `statistics.metricDefinitions.${index}`;
    if (!/^custom_[a-zA-Z0-9_]+$/u.test(metric.name || '') || metrics.has(metric.name)) add(path, '指标名需以 custom_ 开头且不可重复', 'statistics');
    metrics.add(metric.name);
    if (metric.type === 'gauge' && !(metric.gaugeStaleSeconds > 0)) add(path, 'Gauge 必须设置观测过期时间', 'statistics');
    if (metric.type === 'histogram') {
      const buckets = metric.histogramBuckets || [];
      if (!buckets.length || buckets.some((v, i) => !Number.isFinite(v) || (i > 0 && v <= buckets[i - 1]))) add(path, 'Histogram 分桶必须为严格递增的有限数字', 'statistics');
    }
  }
  return errors;
}
export const EVENT_STATES = ['pre','eligible','batch','assigned','hold','match_conflict','terminal'];
export const ENDPOINT_STATES = ['receiving','disabled','paused','globalDisabled','needs_review'];
export const STATUS_LABELS = {conversationStopped:'当前会话已手动停止，自动模型派发暂停',pre:'待前置处理',batch:'已封批待派发',hold:'暂存待处理',receiving:'接收可用',globalDisabled:'全局关闭',open:'轮次处理中',closing:'正在终结',collecting:'收集中',sealed:'已封闭',delivered:'已交付',closed:'已关闭',pending:'待执行',reserved:'已预留执行',succeeded:'阶段执行成功',pending_pre:'待前置处理', eligible:'待收集', batched:'已封批待派发', assigned:'已归属运行', held:'暂存待处理', terminal:'已终结', queued:'已排队', running:'运行中', finalized:'轮次已终结', interrupted:'运行中断', registered:'等待已登记', satisfied:'匹配条件已满足', timed_out:'等待超时', accepting: '接收可用', disabled: '未启用', global_disabled: '全局关闭', paused: '派发暂停', deleted: '已删除', needs_review: '待核实', completed: '已报告完成', skipped: '已跳过', failed: '失败', unknown: '结果未知', expired: '已过期', cancelled: '已取消', protocol_incomplete: '回执不完整', not_delivered: '未交付', waiting: '等待外部事件', match_conflict: '等待匹配冲突'};
export const statusLabel = value => STATUS_LABELS[value] || value || '—';
export function tabKey(event, current, tabs) {
  const keys = tabs.map(item => Array.isArray(item) ? item[0] : item);
  const index = keys.indexOf(current);
  const next = {ArrowRight: (index + 1) % keys.length, ArrowLeft: (index - 1 + keys.length) % keys.length, Home: 0, End: keys.length - 1}[event.key];
  if (next === undefined) return current;
  event.preventDefault(); return keys[next];
}
export function targetTree(items) {
  const nodes = new Map(), roots = [];
  for (const item of items) {
    const folder = (item.kind || item.type) === 'folder';
    const id = item.id || item.folderId || item.conversationUuid;
    if (!folder && (item.archived || item.deleted || item.available === false)) continue;
    nodes.set((folder ? 'folder:' : 'conversation:') + id, {value:folder ? `folder:${id}` : id,label:item.name || item.title || '未命名会话',path:item.path,
      selectable:!folder,disabled:folder || item.selectable === false,...(folder ? {children:[]} : {})});
  }
  // The API already uses the normal tree order. Attach in that order, including
  // interleaved folders/conversations; never alphabetically re-sort either kind.
  for (const item of items) {
    const folder = (item.kind || item.type) === 'folder', id = item.id || item.folderId || item.conversationUuid;
    const node = nodes.get((folder ? 'folder:' : 'conversation:') + id); if (!node) continue;
    const parent = nodes.get('folder:' + (item.parentId || item.parentFolderId || (!folder ? item.folderId : '')));
    (parent?.children || roots).push(node);
  }
  return roots;
}
