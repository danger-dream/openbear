// Collapsed tool rows describe the requested action, never claim it succeeded.
// Keep identifiers/payloads untouched in the expanded detail. Unknown tools and
// operations fall back to their supplied names rather than an invented meaning.
const TOOL_LABELS = {
	Bash: '终端', Read: '读取', Write: '写入', Edit: '修改', EditBatch: '修改',
	Browser: '浏览器', History: '查阅历史', AgentHistory: '查阅历史',
	Memory: '记忆', TaskMemory: '会话记忆', Process: '进程', OpenBearControl: 'OpenBear 控制',
	UserInteraction: '用户交互', ContextCompaction: '上下文压缩',
	Agent: '助手', AgentContinue: '继续助手', AgentInfo: '助手信息', AgentMessage: '助手消息',
	AgentStop: '停止助手', AgentWait: '等待助手', AgentPlanDecision: '计划审批',
	AgentPlanSubmit: '提交计划', AgentPlanProgress: '计划进度',
	WebSearch: '搜索', WebExtract: '阅读网页',
	mcp__parrot__web_search: '搜索', mcp__parrot__web_fetch: '阅读网页',
	mcp__parrot__image_generate: '生成图片', mcp__parrot__image_edit: '编辑图片',
	mcp__parrot__video_generate: '生成视频', mcp__parrot__video_status: '视频进度',
};
export function toolDisplayLabel(name, fallback = '') {
	return TOOL_LABELS[name] || fallback || name || '工具';
}
const text = (value, limit = 96) => {
	if (typeof value !== 'string' && typeof value !== 'number') return '';
	const valueText = String(value).replace(/\s+/g, ' ').trim();
	return valueText.length > limit ? `${valueText.slice(0, limit - 1)}…` : valueText;
};
const object = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
const join = (...parts) => parts.filter(Boolean).join(' · ');
const quoted = value => text(value) ? `“${text(value)}”` : '';
const count = value => value !== '' && value != null && typeof value !== 'boolean' && Number.isInteger(Number(value)) && Number(value) >= 0 ? Number(value) : null;
const targetLabel = value => ({current: '当前对话', current_run: '当前任务', current_task: '当前任务', conversation: '指定对话', explicit: '指定对话', all: '全部', root: '主控', main: '主浏览器', isolated: '独立浏览器'})[value] || text(value, 48);
const limitHint = (value, unit = '条') => count(value) !== null ? `最多 ${count(value)} ${unit}` : '';

function historySummary(data) {
	const action = text(data.action);
	const execution = data.source === 'execution';
	const scope = data.currentTask ? '当前任务' : data.conversationUuid || data.conversation_uuid
		? '指定对话' : targetLabel(data.scope) || '当前对话';
	const subject = `${scope}的${execution ? '执行记录' : '历史'}`;
	if (action === 'search') return join(`搜索${subject}${data.query ? `：${quoted(data.query)}` : ''}`, limitHint(data.limit));
	if (action === 'list') return join('列出对话历史', limitHint(data.limit));
	if (action === 'index') return join(`查看${subject}索引`, limitHint(data.limit));
	if (action === 'read_event' || action === 'read_events') {
		const size = Array.isArray(data.eventIds) ? data.eventIds.length : null;
		return `读取${scope}${size !== null ? ` ${size} 条` : '的指定'}执行记录`;
	}
	if (action === 'read_turn') {
		const range = [count(data.before) > 0 ? `前 ${count(data.before)} 轮` : '', count(data.after) > 0 ? `后 ${count(data.after)} 轮` : ''].filter(Boolean).join('、');
		return join(`读取${scope}的指定轮次`, range ? `含${range}` : '');
	}
	if (action === 'read') {
		if (data.opId) return `读取${scope}的指定消息`;
		if (execution) return `读取${scope}的执行记录`;
		const turns = count(data.from === 'start' ? data.turns : data.lastTurns ?? data.turns);
		const range = data.from === 'start' ? '最早' : '最近';
		const skip = count(data.offset);
		return join(`读取${scope}${turns !== null ? `${range} ${turns} 轮内容` : '的历史内容'}`, skip ? `跳过 ${skip} 轮` : '');
	}
	return action ? join(action, subject) : '';
}

function memorySummary(data) {
	const resource = ({entry:'记忆',doc:'文档',secret:'凭证',identity:'身份'})[data.resource || data.kind] || text(data.resource || data.kind) || '记忆';
	const action = text(data.action);
	const name = text(data.ref || data.name || (data.id ? `#${data.id}` : ''));
	const subject = `${name ? `${name} ` : ''}${resource}`;
	if (action === 'list') return join(`列出${resource}列表`, data.category ? `${text(data.category)} 分类` : '');
	if (action === 'get') return `读取 ${subject}内容`;
	if (action === 'set') return `写入 ${subject}内容`;
	if (['del','delete'].includes(action)) return `删除 ${subject}`;
	return action ? join(action, subject) : `${resource}操作`;
}

function pageAddress(value) {
	if (value === 'about:blank') return '空白';
	if (typeof value !== 'string' || !value) return '';
	try {
		const url = new URL(value);
		if (['http:','https:'].includes(url.protocol)) return text(`${url.host}${url.pathname === '/' ? '' : url.pathname}`, 80);
		if (url.protocol === 'data:') return '内嵌内容';
	} catch { /* Retain a supplied non-URL target as text. */ }
	return text(value, 80);
}
const BROWSER_ACTIONS = {status:'浏览器状态',page:'页面管理',snapshot:'页面快照',capture:'页面截图',find:'页面查找',act:'页面交互',navigate:'页面导航',wait:'等待条件',files:'文件操作',dialog:'对话框',console:'控制台日志',network:'网络请求',evaluate:'页面脚本',recover:'浏览器恢复',describe:'工具用法'};
const ELEMENT_ACTIONS = {click:'点击',fill:'填写',type:'输入',press:'按键',check:'勾选',select:'选择',hover:'悬停',drag:'拖动',scroll:'滚动',resize:'调整窗口尺寸',emulate:'模拟设备'};
function elementTarget(value) {
	const target = text(value, 60);
	if (!target) return '指定元素';
	return target.startsWith('css=') ? `元素 ${target.slice(4)}` : target;
}
function browserSummary(data) {
	const action = text(data.action), params = object(data.params), op = text(params.op);
	const address = pageAddress(params.url);
	switch (action) {
		case 'status': return '查看浏览器状态';
		case 'page':
			if (op === 'new') return `新建${params.mode === 'isolated' ? '独立' : ''} ${address || '空白'} 页面`;
			if (op === 'list') return '列出已打开的页面';
			if (op === 'adopt') return '接管指定页面';
			if (op === 'close') return '关闭指定页面';
			if (op === 'release') return '释放独立浏览器';
			return join('管理页面', op);
		case 'navigate':
			if (op === 'back') return '返回上一页';
			if (op === 'reload') return '刷新页面';
			return address ? `打开 ${address} 页面` : '打开页面';
		case 'snapshot': return params.interactive ? '查看页面交互元素快照' : '查看页面内容快照';
		case 'capture': return params.fullPage ? '截取完整页面图片' : '截取页面图片';
		case 'find': return `查找页面内容${params.text ? `：${quoted(params.text)}` : ''}`;
		case 'describe': return `查看${BROWSER_ACTIONS[params.action] || text(params.action) || '浏览器工具'}的用法`;
		case 'act': {
			if (op === 'press') return `按下 ${text(params.key) || '指定按键'}`;
			if (op === 'resize') return join('调整窗口尺寸', params.width && params.height ? `${params.width} × ${params.height}` : '');
			if (op === 'emulate') return '调整页面显示模拟';
			if (op === 'fill' && Array.isArray(params.fields)) return `填写 ${params.fields.length} 个表单字段`;
			if (op === 'scroll') return '滚动页面';
			const verb = op === 'check' && params.checked === false ? '取消勾选' : ELEMENT_ACTIONS[op] || op || '操作';
			return `${verb} ${elementTarget(params.target)}${op === 'drag' && params.to ? ` → ${elementTarget(params.to)}` : ''}`;
		}
		case 'wait':
			if (params.text) return `等待页面${['hidden','detached'].includes(params.state) ? '不再显示' : '出现'}${quoted(params.text)}`;
			if (params.url) return `等待打开 ${pageAddress(params.url)}`;
			if (params.target) return `等待 ${elementTarget(params.target)}${({visible:'可见',hidden:'隐藏',attached:'出现',detached:'移除'})[params.state] || ''}`;
			return count(params.timeMs) !== null ? `等待 ${Number(params.timeMs) / 1000} 秒` : '等待页面就绪';
		case 'dialog':
			if (op === 'inspect' || !op) return '查看页面对话框';
			if (op === 'handle') return params.accept === false ? '取消页面对话框' : params.accept === true ? '确认页面对话框' : '处理页面对话框';
			return join('处理页面对话框', op);
		case 'files': return ({upload:'上传文件',drop:'拖放文件到页面',list:'查看下载列表',save:'保存下载文件',cancel:'取消文件下载'})[op] || join('页面文件操作', op);
		case 'console': return '查看页面控制台日志';
		case 'network': return params.id ? join('查看指定网络请求', ({'request-headers':'请求头','response-headers':'响应头','request-body':'请求正文','response-body':'响应正文'})[params.part] || text(params.part)) : '查看页面网络请求';
		case 'evaluate': return '执行页面脚本';
		case 'recover': return ({probe:'检查页面连接',connection:'恢复浏览器连接',terminate:'终止页面操作',close:'关闭故障页面',restart:'重启浏览器实例'})[op || 'probe'] || join('恢复浏览器页面', op);
		default: return action;
	}
}

function controlSummary(data) {
	const args = object(data.args), action = text(data.action);
	const labels = {status:'查看运行状态',models:'查看可用模型',mcp_status:'查看 MCP 状态',mcp_reload:'重载 MCP 工具',skills_status:'查看技能状态',skills_reload:'重载技能',restart:'重启服务',new:'新建会话',stop:'停止当前任务',think:'调整思考强度'};
	let label = labels[action] || action;
	if (action === 'think' && args.level) label += `：${({off:'关闭',minimal:'最低',low:'低',medium:'中',high:'高',xhigh:'极高',max:'最高'})[args.level] || text(args.level)}`;
	if (action === 'restart' && count(args.delayS) !== null && Number(args.delayS) > 0) label = `${args.delayS} 秒后${label}`;
	return join(label, text(args.query || data.query));
}

function agentSummary(name, data) {
	const target = targetLabel(data.to || data.agentId || data.taskUuid);
	const description = text(data.description || data.reason || data.message);
	if (name === 'AgentInfo') return join(({list:'列出助手实例',get:'查看助手信息',capabilities:'查看可委派能力'})[data.action] || text(data.action) || '查看助手信息', target);
	if (name === 'AgentWait') return join(data.mode === 'review_after' ? `等待助手，${count(data.reviewAfterSeconds) !== null ? `${data.reviewAfterSeconds} 秒后` : '稍后'}检查进展` : '等待助手事件', text(data.reason));
	if (name === 'AgentPlanDecision') return join(({approve:'批准计划',revise:'要求修改计划',cancel:'取消计划',request_replan:'要求重新规划'})[data.action] || text(data.action), target);
	if (name === 'AgentMessage') return join(`向${target || '助手'}发送消息`, description);
	if (name === 'AgentStop') return join(`停止${target || '助手任务'}`, description);
	if (name === 'AgentContinue') return join(`继续${target || '助手任务'}`, description || text(data.prompt));
	if (name === 'Agent') return description || text(data.prompt);
	return '';
}

// Returns an empty string when a tool has no parameter-aware translator yet.
export function toolActionSummary(name, value) {
	const data = object(value);
	if (!Object.keys(data).length) return '';
	let summary = '';
	if (name === 'Bash') summary = text(data.description) || text(String(data.command || '').split(/\r?\n/, 1)[0]);
	else if (name === 'Read') summary = text(data.description || data.path);
	else if (name === 'Write') summary = text(data.path);
	else if (name === 'Edit') summary = Array.isArray(data.edits) ? `批量编辑 ${data.edits.length} 段` : text(data.path);
	else if (name === 'History' || name === 'AgentHistory') summary = historySummary(data);
	else if (name === 'Memory') summary = memorySummary(data);
	else if (name === 'Browser') summary = browserSummary(data);
	else if (name === 'OpenBearControl') summary = controlSummary(data);
	else if (name.startsWith('Agent')) summary = agentSummary(name, data);
	else if (name === 'Process') summary = join(({list:'列出进程记录',poll:'查询进程状态',log:'读取进程日志',kill:'终止进程',remove:'移除进程记录'})[data.action] || text(data.action), text(data.sessionId || data.session_id));
	else if (name === 'EditBatch') summary = join(text(data.path), Array.isArray(data.edits) ? `修改 ${data.edits.length} 处` : '');
	else if (name === 'UserInteraction') summary = join(({confirm:'请求确认',select:'请求选择',prompt:'请求输入',questionnaire:'请用户填写问卷'})[data.action] || text(data.action), text(data.title));
	else if (name === 'WebSearch' || name === 'mcp__parrot__web_search') summary = join(quoted(data.query), data.freshness ? ({day:'一天内',week:'一周内',month:'一月内',year:'一年内'})[data.freshness] || '' : '');
	else if (name === 'WebExtract' || name === 'mcp__parrot__web_fetch') summary = pageAddress(data.url);
	else if (['mcp__parrot__image_generate','mcp__parrot__image_edit','mcp__parrot__video_generate'].includes(name)) summary = text(data.prompt);
	else if (name === 'mcp__parrot__video_status') summary = join('查询视频生成进度', text(data.request_id, 36));
	return text(summary, 160);
}
