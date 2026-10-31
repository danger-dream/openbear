import {serverName} from './serverNames.js';

// Collapsed tool rows describe the requested action, never claim it succeeded.
// Keep identifiers/payloads untouched in the expanded detail. Unknown tools and
// operations fall back to their supplied names rather than an invented meaning.
const TOOL_LABELS = {
	Bash: '终端', Read: '读取', Write: '写入', Edit: '修改', EditBatch: '修改',
	Browser: '浏览器', History: '查阅历史', AgentHistory: '查阅历史',
	Memory: '记忆', TaskMemory: '会话记忆', WorkItem: '任务', Process: '进程', OpenBearControl: 'OpenBear 控制',
	UserInteraction: '用户交互', ContextCompaction: '上下文压缩',
	Agent: '助手', AgentContinue: '继续助手', AgentInfo: '助手信息', AgentMessage: '助手消息',
	AgentStop: '停止助手', AgentWait: '等待助手', AgentPlanDecision: '计划审批',
	AgentPlanSubmit: '提交计划', AgentPlanProgress: '计划进度',
	WebSearch: '搜索', WebExtract: '阅读网页',
	mcp__parrot__web_search: '搜索', mcp__parrot__web_fetch: '阅读网页',
	mcp__parrot__image_generate: '生成图片', mcp__parrot__image_edit: '编辑图片',
	mcp__parrot__video_generate: '生成视频', mcp__parrot__video_status: '视频进度',
	InstanceControl: '实例管理', Servers: '服务器',
	ConversationRequest: '会话请求', ConversationGroup: '小组',
};
const REQUEST_OUTCOMES = {completed: '完成', needs_input: '需要补充', rejected: '拒绝'};

function conversationRequestSummary(data) {
	switch (data.action) {
		case 'send': return join('发出请求', quoted(data.title));
		case 'supply': return '补充对方要的材料';
		case 'respond': return `声明结果：${REQUEST_OUTCOMES[data.outcome] || text(data.outcome) || '完成'}`;
		case 'cancel': return '撤回请求';
		case 'list': return '列出会话请求';
		case 'get': return '查看会话请求';
		default: return text(data.action);
	}
}

function conversationGroupSummary(data) {
	const group = object(data.group);
	switch (data.action) {
		case 'propose': return join('提议建立小组', quoted(group.name), Array.isArray(group.members) ? `${group.members.length} 位成员` : '');
		case 'add_members': return join('提议新增成员', Array.isArray(data.members) ? `${data.members.length} 位` : '');
		case 'update': return '提议修改小组设定';
		case 'remove_member': return join('提议移除成员', text(data.reason));
		case 'list': return '列出小组';
		case 'get': return '查看小组';
		default: return text(data.action);
	}
}
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
// The Browser tool names a node by server name or id.
function nodeBrowser(value) {
	const node = text(value, 60);
	return node ? `“${text(serverName(node) || node, 40)}”的浏览器` : '';
}

function browserSummary(data) {
	const action = text(data.action), params = object(data.params), op = text(params.op);
	const address = pageAddress(params.url), node = nodeBrowser(params.node);
	switch (action) {
		case 'status': return '查看浏览器状态';
		case 'page':
			if (op === 'new') return `${node ? `在${node}里` : ''}新建${params.mode === 'isolated' ? '独立' : ''} ${address || '空白'} 页面`;
			if (op === 'list') return node ? `列出${node}里的页面` : '列出已打开的页面';
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
		case 'recover': {
			if (node && op === 'connection') return `恢复${node}的连接`;
			if (node && op === 'restart') return `重启${node}`;
			return ({probe:'检查页面连接',connection:'恢复浏览器连接',terminate:'终止页面操作',close:'关闭故障页面',restart:'重启浏览器实例'})[op || 'probe'] || join('恢复浏览器页面', op);
		}
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

const INSTANCE_OUTCOMES = {completed: '完成', failed: '失败', needs_verification: '待检查'};
const INSTANCE_METHODS = {GET: '读取', POST: '提交', PUT: '写入', PATCH: '修改', DELETE: '删除'};
// Recognizes what a remote command does when the model gave no description.
function remoteCommand(command) {
	const value = String(command || '');
	if (/docker\s+compose\b[^\n]*\bup\b/.test(value)) return '启动容器';
	if (/docker\s+compose\b[^\n]*\b(down|stop)\b/.test(value)) return '停止容器';
	if (/docker\s+(build|buildx\s+build)\b/.test(value)) return '构建镜像';
	if (/docker\s+(logs|compose\b[^\n]*\blogs)\b/.test(value)) return '查看容器日志';
	if (/docker\s+(ps|inspect|version|info)\b/.test(value)) return '检查 Docker 环境';
	const first = value.split(/\r?\n/).map(line => line.trim()).find(line => line && !/^(set\s+-|#|printf\s)/.test(line));
	return first ? `执行命令：${first}` : '执行命令';
}
function instanceSummary(data) {
	const p = object(data.params);
	switch (data.action) {
		case 'describe': return '查看实例工具用法';
		case 'list': return '查看实例列表';
		case 'status': return p.instanceId ? '查看实例状态' : '查看本次部署目标';
		case 'operations': return '查看部署记录';
		case 'events': return '查看登录与运行事件';
		case 'verify': return '检查实例运行状态';
		case 'deliver': return p.image ? join('交付部署文件', `使用镜像 ${text(p.image, 48)}`) : '交付部署文件和同版本源码';
		case 'remote': return text(p.description) || remoteCommand(p.command);
		case 'apply_initial_config': return '写入初始模型配置';
		case 'request': return join(`${INSTANCE_METHODS[String(p.method || 'GET').toUpperCase()] || '请求'}实例配置`, text(p.path, 60));
		case 'deploy': return '开始重新部署';
		case 'maintenance': return join('开始维护', text(p.instructions));
		case 'archive': return '归档实例';
		case 'report': return join(`报告结果：${INSTANCE_OUTCOMES[p.outcome] || text(p.outcome)}`, text(p.summary));
		case 'gateway_status': return '查看访问网关状态';
		case 'gateway_sync': return '同步账号到访问网关';
		case 'gateway_update': return '更新访问网关';
		default: return text(data.action);
	}
}

const METRIC_RANGES = {live: '最近 10 分钟', '1h': '最近 1 小时', '24h': '最近 24 小时', '7d': '最近 7 天', '30d': '最近 30 天'};
const PROFILE_FIELDS = {group: '分组', region: '地区', provider: '服务商', remark: '备注', sort: '顺序', billing: '账单', traffic: '流量', access: '接入点', alias: '名称'};
const SETTINGS_FIELDS = {accessPoints: '接入点', defaultAccess: '默认接入点', timezone: '时区', reminders: '提醒规则'};
const MESH_DRAFT_PARTS = {settings: '网格设置', members: '成员', relayLinks: '中转互联', directLinks: '直连', rules: '访问规则'};
export const BENCH_TESTS = {ipcheck: 'IP.Check.Place', netcheck: 'Net.Check.Place', netroute: 'Net.Check.Place 回程路由', tcpquality: 'TCPQuality',
	hwcheck: 'Hardware.Check.Place', geekbench5: 'Geekbench 5', unlock: 'Service Unlock'};
export const BENCH_STATES = {queued: '排队中', preparing: '准备中', running: '运行中', collecting: '收集结果', done: '完成', failed: '失败', stopped: '已停止', interrupted: '中断'};
const BENCH_OPTIONS = {family: {4: '仅 IPv4', 6: '仅 IPv6', 46: 'IPv4 + IPv6'}, mode: {low: '低数据模式', full: '完整模式', v4: '三网质量 IPv4', v6: '三网质量 IPv6',
	'route-v4': '回程线路 IPv4', 'route-v6': '回程线路 IPv6', intl: '国际互联', speedtest: '三网测速', all: '完整检测'}};
const BENCH_PARTS = {text: '读取评测报告', json: '读取评测结果数据', log: '读取评测运行日志'};
// A benchmark's options in a few words: 完整模式, IPv4 + IPv6, 广东, 生成在线报告.
function benchOptions(options) {
	const o = object(options);
	return ['family', 'mode'].map(k => BENCH_OPTIONS[k][o[k]] || '').concat(text(o.province, 12), o.region && o.region !== 'auto' ? `地区 ${text(o.region, 12)}` : '',
		o.report === true ? '生成在线报告' : '').filter(Boolean).join('、');
}
const firstLine = value => String(value || '').split(/\r?\n/).map(line => line.trim()).find(Boolean) || '';
const sshLabel = ssh => {
	const value = object(ssh);
	return value.host ? `${value.username ? `${text(value.username, 32)}@` : ''}${text(value.host, 64)}${value.port && Number(value.port) !== 22 ? `:${value.port}` : ''}` : '';
};
// The named server, or what the targets add up to, for a sentence like “在…上”.
function serverRef(id) {
	const name = serverName(id);
	return name ? `“${text(name, 40)}”` : '指定服务器';
}
function serverTargets(p) {
	if (p.all === true) return '全部在线服务器';
	if (text(p.group)) return `“${text(p.group, 30)}”分组的在线服务器`;
	const ids = Array.isArray(p.serverIds) ? p.serverIds : p.serverId ? [p.serverId] : [];
	if (ids.length === 1) return serverRef(ids[0]);
	const named = ids.map(serverName).filter(Boolean);
	if (ids.length && named.length === ids.length && ids.length <= 3) return named.map(n => `“${text(n, 24)}”`).join('、');
	return ids.length ? `${ids.length} 台服务器` : '指定服务器';
}
const pathsOf = p => (Array.isArray(p.paths) ? p.paths : p.path ? [p.path] : []).filter(v => typeof v === 'string');
const pathList = paths => paths.length === 1 ? text(paths[0], 80) : paths.length ? `${text(paths[0], 60)} 等 ${paths.length} 项` : '';

function fileOpSummary(p) {
	const where = `在${serverRef(p.serverId)}上`, paths = pathsOf(p);
	switch (p.op) {
		case 'mkdir': return join(`${where}新建文件夹${quoted(p.name)}`, text(p.path, 80));
		case 'touch': return join(`${where}新建文件${quoted(p.name)}`, text(p.path, 80));
		case 'rename': return join(`${where}重命名`, `${text(p.path, 80)} → ${text(p.name, 40)}`);
		case 'chmod': return join(`${where}把权限改为 ${text(p.mode, 8)}${p.recursive === true ? '（含子目录）' : ''}`, pathList(paths));
		case 'compress': return join(`${where}压缩${paths.length > 1 ? ` ${paths.length} 项` : ''}为${quoted(`${text(p.name, 40)}${p.format ? `.${p.format}` : ''}`)}`, pathList(paths));
		case 'extract': return join(`${where}解压 ${text(p.path, 80)}`, p.dest ? `到 ${text(p.dest, 60)}` : '');
		case 'delete': return join(`${where}删除`, pathList(paths));
		default: return join(`${where}执行文件操作`, text(p.op));
	}
}

function serversSummary(data) {
	const p = object(data.params), on = serverRef(p.serverId);
	const purpose = text(p.purpose);
	switch (data.action) {
		case 'describe': return p.topic === 'bench' ? '查看评测用法' : p.topic === 'files' ? '查看文件传输用法' : '查看服务器工具用法';
		case 'list': return join('查看服务器列表', p.group ? `“${text(p.group, 30)}”分组` : '');
		case 'get': return `查看${on}的详情`;
		case 'metrics': return join(`查看${on}的监控数据`, METRIC_RANGES[p.range || '1h'] || text(p.range));
		case 'exec': {
			const command = firstLine(p.command);
			return join(purpose || (command ? `执行 ${text(command, 80)}` : '执行命令'), `在${serverTargets(p)}上`);
		}
		case 'list_dir': return `列出${on}的${p.path === '/' || !p.path ? '根目录' : ` ${text(p.path, 80)} `}`.replace(/ $/, '');
		case 'read_file': return join(`${p.encoding === 'base64' ? '下载' : '读取'}${on}上的文件`, text(p.path, 80));
		case 'write_file': return join(`${typeof p.contentBase64 === 'string' && p.content == null ? '上传' : '写入'}${on}上的文件`, text(p.path, 80), purpose);
		case 'edit_file': return join(`修改${on}上的文件`, text(p.path, 80), Array.isArray(p.edits) ? `${p.edits.length} 处` : '', purpose);
		case 'file_op': return join(fileOpSummary(p), purpose);
		case 'file_op_status': return `查询${on}上文件操作的进度`;
		case 'upload_file': return join(`上传文件到${on}`, text(p.remotePath, 80), purpose);
		case 'download_file': return join(`从${on}下载文件`, text(p.remotePath, 80), purpose);
		case 'transfer_status': return join(`查询${on}的文件传输进度`, text(p.transferId, 40));
		case 'transfer_cancel': return join(`取消${on}的文件传输`, text(p.transferId, 40));
		case 'job_start': {
			const command = firstLine(p.command);
			return join(purpose || `在${on}上启动后台任务`, command ? text(command, 80) : '');
		}
		case 'jobs': return `查看${on}的后台任务`;
		case 'job_status': return join(`查看${on}的后台任务`, text(p.jobId, 40));
		case 'job_wait': return join(`等待${on}的后台任务`, text(p.jobId, 40), text(p.until) ? `直到输出匹配 ${text(p.until, 40)}` : '');
		case 'job_stop': return join(`停止${on}的后台任务`, text(p.jobId, 40));
		case 'update_node': return `更新${p.all === true ? '全部服务器' : serverTargets(p)}的节点`;
		case 'edit': {
			const fields = [p.name != null ? '名称' : '', ...Object.keys(object(p.profile)).map(k => PROFILE_FIELDS[k] || k)].filter(Boolean);
			return join(`修改${on}`, fields.join('、'));
		}
		case 'renew': return `为${on}续费一个周期`;
		case 'add': return join(`添加服务器${quoted(p.name)}`, sshLabel(p.ssh) ? `SSH ${sshLabel(p.ssh)}` : '', p.install === true ? `并安装节点${p.browser === true ? '（开启浏览器）' : ''}` : '');
		case 'install': return `通过 SSH 在${on}上安装节点${p.browser === true ? '并开启浏览器' : ''}`;
		case 'install_command': return `生成${on}的安装命令${p.browser === true ? '（开启浏览器）' : ''}`;
		case 'batch_install': return join(`批量添加并安装 ${Array.isArray(p.servers) ? p.servers.length : ''} 台服务器`.replace('  ', ' '),
			object(p.ssh).username ? `SSH 用户 ${text(p.ssh.username, 32)}` : '', p.browser === true ? '并开启浏览器' : '');
		case 'batch_install_status': return '查询批量安装进度';
		case 'set_ssh': return join(`设置${on}的 SSH 连接`, sshLabel(p.ssh));
		case 'check_ssh': return `测试${on}的 SSH 连接`;
		case 'remove_ssh': return `移除${on}的 SSH 连接`;
		case 'ssh_keys': return '查看可用的 SSH 密钥';
		case 'remove_node': return `移除${on}的节点${p.uninstall === true ? '并卸载' : ''}`;
		case 'delete': return `删除服务器${on}`;
		case 'reorder': return join('调整服务器顺序', Array.isArray(p.ids) ? `${p.ids.length} 台` : '');
		case 'commands': return '查看常用命令';
		case 'command_save': return join(`${p.commandId != null ? '修改' : '添加'}常用命令${quoted(p.name)}`, text(firstLine(p.command), 60));
		case 'command_delete': return '删除一条常用命令';
		case 'command_order': return '调整常用命令顺序';
		case 'settings': return '查看服务器设置';
		case 'save_settings': return join('修改服务器设置', Object.keys(p).map(k => SETTINGS_FIELDS[k]).filter(Boolean).join('、'));
		case 'test_access': return join('测试接入点', pageAddress(p.url));
		case 'push_access': return '把在线节点切换到设置的接入点';
		case 'codes': return '查看注册码';
		case 'code_create': return join(`创建注册码${quoted(p.name)}`, p.group ? `“${text(p.group, 30)}”分组` : '',
			count(p.maxUses) ? `最多用 ${p.maxUses} 次` : '', count(p.expiresInDays) ? `${p.expiresInDays} 天后过期` : '');
		case 'code_revoke': return '作废注册码';
		case 'reminders': return '查看提醒记录';
		case 'test_reminder': return '发送测试提醒';
		case 'browser': return `查看${on}的浏览器`;
		case 'set_browser':
			if (p.enabled === false) return `关闭${on}的浏览器`;
			return join(`开启${on}的浏览器`, text(p.endpoint) ? `连接 ${text(p.endpoint, 60)}` : p.download === true ? '使用下载的测试版 Chrome' : '');
		case 'restart_browser': return `重启${on}的浏览器`;
		case 'mesh': return '查看组网概况';
		case 'mesh_draft': return '查看组网草稿';
		case 'mesh_save_draft': return join('修改组网草稿', Object.keys(MESH_DRAFT_PARTS).filter(k => k in p).map(k => MESH_DRAFT_PARTS[k]).join('、'));
		case 'mesh_join': return `把${serverTargets(p)}加入组网`;
		case 'mesh_leave': return `把${on}移出组网`;
		case 'mesh_apply': return '应用组网草稿';
		case 'mesh_discard': return '放弃组网草稿';
		case 'mesh_rollback': return '回退到上一个组网版本';
		case 'mesh_member': return `查看${on}的组网状态`;
		case 'mesh_trend': return join(`查看${on}的组网趋势`, METRIC_RANGES[p.range || '1h'] || text(p.range));
		case 'mesh_path': return p.dst ? `查看${serverRef(p.src)}到${serverRef(p.dst)}的组网路径` : `查看${serverRef(p.src)}到各成员的组网路径`;
		case 'bench_catalog': return '查看评测项目';
		case 'bench_runs': return join('查看评测记录', p.serverId ? on : '', BENCH_TESTS[p.test] || text(p.test, 30), BENCH_STATES[p.state] || '');
		case 'bench_start': return p.suite === true ? join(`在${on}上运行综合评测`, benchOptions(p.options))
			: join(`在${on}上运行 ${BENCH_TESTS[p.test] || text(p.test, 30) || '评测'}`, benchOptions(p.options));
		case 'bench_wait': return '等待评测结束';
		case 'bench_get': return BENCH_PARTS[p.part] || '读取评测报告';
		case 'bench_stop': return '停止评测';
		case 'bench_delete': return '删除评测记录';
		default: return text(data.action);
	}
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
	else if (name === 'InstanceControl') summary = instanceSummary(data);
	else if (name === 'Servers') summary = serversSummary(data);
	else if (name === 'ConversationRequest') summary = conversationRequestSummary(data);
	else if (name === 'ConversationGroup') summary = conversationGroupSummary(data);
	else if (name === 'mcp__parrot__video_status') summary = join('查询视频生成进度', text(data.request_id, 36));
	return text(summary, 160);
}
