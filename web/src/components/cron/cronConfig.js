import {normalizedRunDefaults, updateRunDefault} from '../folderRunDefaults.js';

export const OUTCOMES = ['completed', 'failed', 'timed_out', 'tokens_exceeded', 'cost_exceeded', 'cancelled'];
export const STATUSES = ['starting', 'running', ...OUTCOMES, 'interrupted', 'post_failed', 'missed'];
export const TABS = [['basic', '基本配置'], ['schedule', '时间规则'], ['instructions', '处理指令'], ['scripts', '脚本']];
export const PAGE_TABS = [['calendar', '任务日历'], ['jobs', '任务列表'], ['history', '运行历史']];
// Keep draft text intact while typing; the API receives only an integer or null.
export function parseTokenBudget(value) {
  if (value == null || (typeof value === 'string' && !value.trim())) return null;
  if (typeof value === 'number') return value;
  if (typeof value !== 'string') return NaN;
  const match = value.trim().match(/^(\d+(?:\.\d*)?|\.\d+)\s*([kmb])?$/i);
  if (!match) return NaN;
  const exponent = {k:3,m:6,b:9}[match[2]?.toLowerCase()] || 0;
  return Number(`${match[1]}e${exponent}`);
}
const COMMON_TIMEZONES = {
  UTC:'协调世界时', 'Asia/Shanghai':'北京 / 上海', 'Asia/Hong_Kong':'香港', 'Asia/Taipei':'台北',
  'Asia/Singapore':'新加坡', 'Asia/Tokyo':'东京', 'Asia/Seoul':'首尔', 'Asia/Kolkata':'印度',
  'Asia/Dubai':'迪拜', 'Asia/Bangkok':'曼谷', 'Asia/Jakarta':'雅加达', 'Europe/London':'伦敦',
  'Europe/Paris':'巴黎', 'Europe/Berlin':'柏林', 'Europe/Moscow':'莫斯科',
  'America/New_York':'纽约', 'America/Chicago':'芝加哥', 'America/Denver':'丹佛', 'America/Los_Angeles':'洛杉矶',
  'America/Toronto':'多伦多', 'America/Vancouver':'温哥华', 'America/Sao_Paulo':'圣保罗',
  'Australia/Sydney':'悉尼', 'Australia/Perth':'珀斯', 'Pacific/Auckland':'奥克兰',
  'Pacific/Honolulu':'檀香山', 'Africa/Cairo':'开罗', 'Africa/Johannesburg':'约翰内斯堡',
};
export function timezoneOptions(current, intl = Intl) {
  const zones = [...Object.keys(COMMON_TIMEZONES), ...(intl.supportedValuesOf?.('timeZone') || []), current].filter(Boolean);
  return [...new Set(zones)].map(value => ({value,label:COMMON_TIMEZONES[value] ? `${value} · ${COMMON_TIMEZONES[value]}` : value}));
}
export const INTERVAL_UNITS = [{value:1,label:'秒'},{value:60,label:'分'},{value:3600,label:'时'},{value:86400,label:'天'}];
export function intervalUnitFor(seconds) {
  return [...INTERVAL_UNITS].reverse().find(unit => Number.isInteger(seconds) && seconds > 0 && seconds % unit.value === 0)?.value || 1;
}
export function pickerDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date : null;
}
export function pickerIso(value) {
  return value && Number.isFinite(value.getTime?.()) ? value.toISOString() : null;
}
const labels = {starting:'启动中', running:'运行中', completed:'已完成', failed:'失败', timed_out:'执行超时', tokens_exceeded:'Tokens 超限', cost_exceeded:'金额超限', cancelled:'已取消', interrupted:'已中断', post_failed:'后置失败', missed:'已错过', armed:'已就绪', disabled:'已停用', exhausted:'计划已结束', waiting_calendar:'等待日历数据', target_missing:'目录不可用', deleted:'已删除', pre:'前置脚本', model:'模型执行', post:'后置脚本', finished:'结束', scheduled:'定时', manual:'手动'};
export const label = value => labels[value] || value || '—';
export const clone = value => JSON.parse(JSON.stringify(value));
export const message = error => error?.response?.data?.message || error?.message || '请求失败，请重试';
export const number = value => value == null ? '—' : Number(value).toLocaleString('zh-CN', {maximumSignificantDigits: 15});
export const money = value => value == null ? '—' : `$${number(value)}`;
export const time = value => value == null || value === '' ? '—' : new Date(value).toLocaleString('zh-CN', {hour12:false});
export function scriptDefaults(post = false) {
  return {enabled:false, runtime:'python', code:'', timeoutSeconds:30, onError:'fail', retry:{maxAttempts:1, backoffSeconds:[2,10,30,60]}, ...(post ? {onOutcomes:[...OUTCOMES]} : {})};
}
export function defaultConfig() {
  return {schedule:{kind:'cron',timezone:'Asia/Shanghai',expression:'0 9 * * *',at:null,everySeconds:null,anchorAt:null}, runConfig:{mainModel:null,mainThinkingLevel:null,mainFastMode:null}, instructions:'', allowServerChanges:false, timeoutSeconds:null,totalTokensBudget:null,costBudgetUsd:null,notifications:{mode:'inherit',errorsOnly:false},conversationRetention:{keepLast:null,keepDays:null},pre:scriptDefaults(),post:scriptDefaults(true)};
}
export function draftOf(job, folderId = '') {
  const config = clone(job?.config || defaultConfig());
  config.conversationRetention = {keepLast:null,keepDays:null,...config.conversationRetention};
  return {folderId:job?.folderId || folderId,name:job?.name || '',description:job?.description || '',enabled:job?.enabled ?? false,config};
}
export function schedulePayload(schedule) {
  const bounds=schedule.kind!=='at' && (schedule.startAt || schedule.endAt) ? {startAt:schedule.startAt || null,endAt:schedule.endAt || null} : {};
  const extras={...(schedule.kind==='cron' && schedule.second!=null?{second:schedule.second}:{}),...(schedule.skipHolidays?{skipHolidays:true}:{})};
  return {...bounds,...extras,kind:schedule.kind,timezone:schedule.timezone,expression:schedule.kind === 'cron' ? schedule.expression : null,at:schedule.kind === 'at' ? schedule.at : null,everySeconds:schedule.kind === 'every' ? schedule.everySeconds : null,anchorAt:schedule.kind === 'every' ? schedule.anchorAt || null : null};
}
export function scheduleText(schedule = {}) {
  const rest=schedule.skipHolidays?' · 跳过休息日（含调休）':'';
  const bounds=schedule.startAt || schedule.endAt ? ` · ${schedule.startAt?time(schedule.startAt):'不限开始'} 至 ${schedule.endAt?`${time(schedule.endAt)}（不含）`:'不限结束'}` : '';
  if (schedule.kind === 'at') return `一次性 · ${time(schedule.at)}${rest}`;
  if (schedule.kind === 'every') {
    const unit = INTERVAL_UNITS.find(item => item.value === intervalUnitFor(schedule.everySeconds));
    return `每 ${number(schedule.everySeconds == null ? null : schedule.everySeconds / unit.value)} ${unit.label}${bounds}${rest}`;
  }
  return `${schedule.expression || '—'}${schedule.second?` · 第 ${schedule.second} 秒`:''} · ${schedule.timezone || '—'}${bounds}${rest}`;
}
export const overrides = config => Object.fromEntries(Object.entries(config || {}).filter(([,value]) => value != null));
export function effectiveModel(config, folder, models) {
  return normalizedRunDefaults({local:overrides(config),inherited:{...folder.inherited,...folder.local},fallback:folder.fallback,resolved:folder.resolved}, models);
}
export function inheritedModel(config, folder, models) {
  return Object.fromEntries(['mainModel','mainThinkingLevel','mainFastMode'].map(field => {
    const local = {...config, [field]:null};
    return [field, effectiveModel(local,folder,models)[field]];
  }));
}
export function changeModel(config, field, selection) {
  const next = updateRunDefault(overrides(config),field,selection);
  return {mainModel:next.mainModel ?? null,mainThinkingLevel:next.mainThinkingLevel ?? null,mainFastMode:next.mainFastMode ?? null};
}
const validDate = value => typeof value === 'string' && /(?:Z|[+-]\d{2}:\d{2})$/i.test(value) && Number.isFinite(Date.parse(value));
export function scheduleErrors(schedule) {
  const errors = [];
  const add = (field,text) => errors.push({path:`schedule.${field}`,tab:'schedule',message:text});
  if (!['at','every','cron'].includes(schedule.kind)) add('kind','请选择时间规则');
  try { new Intl.DateTimeFormat('en',{timeZone:schedule.timezone || '!'}); } catch { add('timezone','请选择有效的规则时区'); }
  if (schedule.kind === 'at' && !validDate(schedule.at)) add('at','请选择执行日期和时间');
  if (schedule.kind === 'every') {
    if (!Number.isInteger(schedule.everySeconds) || schedule.everySeconds <= 0) add('everySeconds','间隔须为正整数秒');
    if (schedule.anchorAt && !validDate(schedule.anchorAt)) add('anchorAt','请选择有效的间隔起点');
  }
  if (schedule.kind === 'cron') {
    if (String(schedule.expression || '').trim().split(/\s+/).length !== 5) add('expression','Cron 表达式须为五段；具体规则由服务器预览校验');
    if(schedule.second!=null && (!Number.isInteger(schedule.second) || schedule.second<0 || schedule.second>59))add('second','秒数须为 0–59 的整数');
  }
  if(schedule.kind!=='at') {
    for (const [field,title] of [['startAt','开始时间'],['endAt','结束时间']]) if(schedule[field] && !validDate(schedule[field]))add(field,`请选择有效的${title}`);
    if(schedule.startAt && schedule.endAt && Date.parse(schedule.startAt)>=Date.parse(schedule.endAt))add('endAt','结束时间必须晚于开始时间');
  }
  return errors;
}
export function validateDraft(draft, {models,folder = {},environment = {}} = {}) {
  const errors = scheduleErrors(draft.config.schedule), c = draft.config;
  const add = (path,text,tab = 'basic') => errors.push({path,tab,message:text});
  if (!draft.folderId) add('folderId','请选择所属目录');
  if (!draft.name.trim()) add('name','请填写任务名称');
  for (const [field,title] of [['timeoutSeconds','单次执行超时'],['costBudgetUsd','金额预算']]) {
    const value = c[field];
    if (value != null && (typeof value !== 'number' || !Number.isFinite(value) || value <= 0)) add(field,`${title}须为大于 0 的有效数字`);
  }
  const tokens = parseTokenBudget(c.totalTokensBudget);
  if (tokens != null && (!Number.isSafeInteger(tokens) || tokens <= 0)) add('totalTokensBudget','Tokens 预算须为正整数，可用 k / m / b（例如 1024k、10m），且不能超过安全整数范围');
  if (!['inherit','silent','telegram','push','both'].includes(c.notifications.mode)) add('notifications.mode','请选择通知方式');
  for (const [field,max] of [['keepLast',10000],['keepDays',3650]]) {
    const value = c.conversationRetention[field];
    if (value != null && !(Number.isInteger(value) && value > 0 && value <= max)) add(`conversationRetention.${field}`,`会话保留须为 1–${max} 的整数，或留空`);
  }
  if (draft.enabled && !c.instructions.trim()) add('instructions','启用前请填写执行目标、范围与授权','instructions');
  const rc = c.runConfig;
  if (models) {
    const effective = effectiveModel(rc,folder,models), model = models.find(item => item.key === effective.mainModel);
    if (rc.mainModel != null && !models.some(item => item.key === rc.mainModel)) add('runConfig.mainModel','模型已不可用，请更换或恢复继承');
    if (rc.mainThinkingLevel != null && !model?.thinkingLevels?.includes(rc.mainThinkingLevel)) add('runConfig.mainThinkingLevel','当前模型不支持此思考强度，请恢复继承或重新选择');
    if (rc.mainFastMode === true && !model?.supportsFast) add('runConfig.mainFastMode','当前模型不支持 Fast，请关闭或恢复继承');
  }
  if (![null,true,false].includes(rc.mainFastMode)) add('runConfig.mainFastMode','Fast 必须为继承、开启或关闭');
  for (const phase of ['pre','post']) {
    const script = c[phase];
    const err = (field,text) => add(`${phase}.${field}`,text,'scripts');
    if (script.enabled && !script.code.trim()) err('code','启用脚本前请填写代码');
    if (!['python','node'].includes(script.runtime) || (script.enabled && environment.runtimes?.find(item => item.runtime === script.runtime)?.available === false)) err('runtime','请选择服务器可用的解释器');
    if (!(typeof script.timeoutSeconds === 'number' && Number.isFinite(script.timeoutSeconds) && script.timeoutSeconds > 0 && script.timeoutSeconds <= 3600)) err('timeoutSeconds','脚本超时须大于 0 且不超过 3600 秒，停用时也不能留空');
    if (!['fail','continue'].includes(script.onError)) err('onError','请选择失败策略');
    if (!Number.isInteger(script.retry.maxAttempts) || script.retry.maxAttempts < 1 || script.retry.maxAttempts > 5) err('retry.maxAttempts','总尝试次数须为 1–5 的整数');
    if (!Array.isArray(script.retry.backoffSeconds) || script.retry.backoffSeconds.some(value => !Number.isFinite(value) || value < 0 || value > 3600)) err('retry.backoffSeconds','每次重试等待须为 0–3600 秒，以逗号分隔');
    if (phase === 'post' && (!Array.isArray(script.onOutcomes) || script.onOutcomes.some(value => !OUTCOMES.includes(value)))) err('onOutcomes','请选择支持的业务结果');
  }
  return errors;
}
export function tabKey(event, current, tabs = TABS) {
  if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return current;
  event.preventDefault(); const index = tabs.findIndex(([key]) => key === current);
  return tabs[event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length-1 : (index+(event.key === 'ArrowRight' ? 1 : -1)+tabs.length)%tabs.length][0];
}
