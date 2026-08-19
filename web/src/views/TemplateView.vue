<script setup>
import {computed, onMounted, ref, watch} from "vue";
import {ElMessage, ElMessageBox} from "element-plus";
import {encode} from "gpt-tokenizer";
import MarkdownIt from "markdown-it";
import hljs from "highlight.js";
import "highlight.js/styles/github.css";
import {Api} from "../api";
import MdEditor from "../components/MdEditor.vue";

const templates = ref([]);
const activeId = ref(null);
const editing = ref(null);
const original = ref("");
const showHelp = ref(false);
const showParams = ref(false);

const sampleParams = ref(JSON.stringify({}, null, 2));
const runtimePromptParams = ref(null);

const previewResult = ref("");
const previewError = ref("");
const previewLoading = ref(false);
const previewMs = ref(0);
const previewAt = ref("");
const autoPreview = ref(true);
const previewMode = ref("raw");
let previewTimer = null;
let previewSeq = 0;
let previewRunning = false;
let previewPending = false;

function tokenCount(text) {
	if (!text) return 0;
	try {
		return encode(text).length;
	} catch {
		return Math.ceil(String(text).length / 2);
	}
}

function formatNum(n) {
	return Number(n || 0).toLocaleString();
}

function fmtTime(d = new Date()) {
	return d.toLocaleTimeString("zh-CN", {hour12: false});
}

function resetParamsFromRuntime() {
	sampleParams.value = JSON.stringify(runtimePromptParams.value || {}, null, 2);
}

function parseSampleParams() {
	try {
		const parsed = JSON.parse(sampleParams.value || "{}");
		return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
	} catch {
		return null;
	}
}

function sampleParamsNeedRuntimeRefresh() {
	const current = parseSampleParams();
	if (current === null) return false;
	return !current.builtinToolNames
		|| !current.builtinToolSummaries
		|| !current.mcpToolNames
		|| !current.mcpToolSummaries
		|| !current.mcpServerInstructions
		|| !current.tools?.builtin
		|| !current.tools?.mcp;
}

const dirty = () => editing.value && JSON.stringify(editing.value) !== original.value;
const templateChars = computed(() => editing.value?.content?.length || 0);
const templateTokens = computed(() => tokenCount(editing.value?.content || ""));
const outputChars = computed(() => previewResult.value.length || 0);
const outputTokens = computed(() => tokenCount(previewResult.value || ""));
const previewRatio = computed(() => {
	if (!templateTokens.value) return "—";
	return (outputTokens.value / templateTokens.value).toFixed(2) + "×";
});
const markdown = new MarkdownIt({
	html: false,
	linkify: true,
	typographer: false,
	breaks: false,
	highlight(str, lang) {
		const language = lang && hljs.getLanguage(lang) ? lang : "";
		if (language) {
			try {
				return `<pre class="hljs"><code>${hljs.highlight(str, {
					language,
					ignoreIllegals: true
				}).value}</code></pre>`;
			} catch {
			}
		}
		return `<pre class="hljs"><code>${markdown.utils.escapeHtml(str)}</code></pre>`;
	},
});
markdown.renderer.rules.link_open = (tokens, idx, options, _env, self) => {
	const token = tokens[idx];
	token.attrSet("target", "_blank");
	token.attrSet("rel", "noreferrer");
	return self.renderToken(tokens, idx, options);
};
const renderedPreview = computed(() => markdown.render(previewResult.value || ""));
const activeTemplateName = computed(() => templates.value.find((t) => t.is_active)?.name || "未设置");
const agentActiveTemplateName = computed(() => templates.value.find((t) => t.is_agent_active)?.name || "未设置");

async function load(keepId = activeId.value) {
	const data = await Api.templates();
	templates.value = data.items || [];
	if (data.promptParams) {
		runtimePromptParams.value = data.promptParams;
		if (!sampleParams.value || sampleParams.value === "{}" || sampleParamsNeedRuntimeRefresh()) resetParamsFromRuntime();
	}
	if (keepId) {
		const t = templates.value.find((x) => x.id === keepId);
		if (t) {
			select(t);
			return;
		}
	}
	if (!editing.value && templates.value.length) {
		select(templates.value.find((x) => x.is_active) || templates.value[0]);
	}

}

function select(t) {
	activeId.value = t.id;
	editing.value = {...t};
	original.value = JSON.stringify(editing.value);
	previewResult.value = "";
	previewError.value = "";
	schedulePreview(120);
}

async function selectById(id) {
	if (dirty()) {
		try {
			await ElMessageBox.confirm("当前模板有未保存修改，切换后会丢失这些修改。确定切换？", "切换模板", {
				type: "warning", confirmButtonText: "放弃并切换", cancelButtonText: "继续编辑",
			});
		} catch {
			activeId.value = editing.value?.id || null;
			return;
		}
	}
	const t = templates.value.find((x) => x.id === id);
	if (t) select(t);
}

onMounted(async () => {
	await load();
});

async function save() {
	if (!editing.value) return;
	const r = await Api.updateTemplate(editing.value.id, editing.value);
	if (r?.ok === false) throw new Error(r.error || "保存失败");
	original.value = JSON.stringify(editing.value);
	ElMessage.success("已保存");
	await load(editing.value.id);
}

async function activate() {
	const r = await Api.updateTemplate(editing.value.id, {...editing.value, is_active: 1});
	if (r?.ok === false) throw new Error(r.error || "设置失败");
	ElMessage.success("已设为激活模板");
	await load(editing.value.id);
}

async function activateAgent() {
	const r = await Api.updateTemplate(editing.value.id, {...editing.value, is_agent_active: 1});
	if (r?.ok === false) throw new Error(r.error || "设置失败");
	ElMessage.success("已设为 Agent 提示词");
	await load(editing.value.id);
}

async function newTpl() {
	const r = await Api.createTemplate({name: "新模板", content: SAMPLE, is_active: 0, is_agent_active: 0});
	if (r?.ok === false) throw new Error(r.error || "创建失败");
	await load(r.id);
}

async function removeCurrent() {
	if (!editing.value) return;
	const name = editing.value.name;
	await ElMessageBox.confirm(`确定删除模板「${name}」？不可恢复。`, "删除确认", {
		type: "warning", confirmButtonText: "删除", cancelButtonText: "取消",
	});
	const r = await Api.deleteTemplate(editing.value.id);
	if (r?.ok === false) throw new Error(r.error || "删除失败");
	ElMessage.success("已删除");
	activeId.value = null;
	editing.value = null;
	original.value = "";
	await load(null);
}

async function runPreview({silent = false} = {}) {
	if (!editing.value) return;
	if (previewRunning) {
		previewPending = true;
		return;
	}
	const seq = ++previewSeq;
	const content = editing.value.content || "";
	const name = editing.value.name || "";
	previewRunning = true;
	previewLoading.value = true;
	previewError.value = "";
	const t0 = performance.now();
	try {
		const params = JSON.parse(sampleParams.value || "{}");
		const r = await Api.previewTemplate(params, content, name);
		if (seq === previewSeq) {
			previewResult.value = r.prompt || "";
			previewMs.value = r.ms ?? Math.round(performance.now() - t0);
			previewAt.value = fmtTime();
		}
	} catch (e) {
		if (seq === previewSeq) {
			previewError.value = e?.response?.data?.error || e.message || String(e);
			if (!silent) ElMessage.error("预览失败: " + previewError.value);
		}
	} finally {
		previewRunning = false;
		if (seq === previewSeq) previewLoading.value = false;
		if (previewPending) {
			previewPending = false;
			schedulePreview(80);
		}
	}
}

function schedulePreview(delay = 700) {
	if (!autoPreview.value || !editing.value) return;
	clearTimeout(previewTimer);
	previewTimer = setTimeout(() => runPreview({silent: true}), delay);
}

watch(() => editing.value?.content, () => schedulePreview(), {flush: "post"});
watch(sampleParams, () => schedulePreview(500));
watch(autoPreview, (v) => {
	if (v) schedulePreview(80);
});

const SAMPLE = `You are OpenBear, a capable AI assistant operating inside a private Web console.\n\nWorkspace: [[ workspaceDir ]]\n\n## Tools\n\n### Built-in tools\n[[ helpers.toolLines(builtinToolNames, builtinToolSummaries) ]]\n\n@if mcpToolNames\n### MCP tools\n[[ helpers.toolLines(mcpToolNames, mcpToolSummaries) ]]\n@endif\n\n@if mcpServerInstructions\n### MCP server instructions\n@each item in mcpServerInstructions\n#### [[ item.server ]]\n[[ item.instructions ]]\n@endeach\n@endif`;
</script>

<template>
	<div class="h-full flex flex-col">
		<header
			class="h-14 shrink-0 flex items-center justify-between px-6 border-b border-macborder bg-white/70 backdrop-blur">
			<div class="flex items-center gap-2 min-w-0">
				<h1 class="text-base font-semibold shrink-0">提示词模板</h1>
				<span class="text-xs text-macsub truncate">选择模板 · 编辑 · 自动补全 · 实时预览</span>
			</div>
			<div class="flex gap-2 shrink-0">
				<el-button :icon="'QuestionFilled'" @click="showHelp = true" round>语法说明</el-button>
				<el-button :icon="'Plus'" @click="newTpl" round>新建</el-button>
				<el-button @click="save" round :disabled="!dirty()">保存</el-button>
				<el-button type="primary" @click="activate" round :disabled="editing?.is_active">设为激活</el-button>
				<el-button type="success" @click="activateAgent" round :disabled="editing?.is_agent_active">设为Agent提示词</el-button>
			</div>
		</header>
		
		<div v-if="editing" class="h-14 shrink-0 px-4 border-b border-macborder bg-white/55 flex items-center gap-3">
			<div class="flex items-center gap-2 min-w-0">
				<span class="text-xs text-macsub shrink-0">模板</span>
				<el-select v-model="activeId" @change="selectById" filterable class="!w-72" placeholder="选择模板">
					<el-option v-for="t in templates" :key="t.id"
					           :label="t.name + (t.is_active ? ' · 激活' : '') + (t.is_agent_active ? ' · Agent提示词' : '')"
					           :value="t.id"/>
				</el-select>
				<el-input v-model="editing.name" placeholder="模板名" class="!w-72"/>
			</div>
			
			<div class="flex items-center gap-2 text-xs shrink-0">
				<span v-if="editing.is_active" class="text-green-600">● 当前激活</span>
				<span v-else class="text-macsub">激活: {{ activeTemplateName }}</span>
				<span v-if="editing.is_agent_active" class="text-emerald-600">● Agent提示词</span>
				<span v-else class="text-macsub">Agent: {{ agentActiveTemplateName }}</span>
				<span v-if="dirty()" class="text-orange-500">● 未保存</span>
			</div>
			
			<div class="ml-auto flex items-center gap-2 shrink-0">
				<span class="text-[11px] text-macsub px-2 py-1 rounded-full bg-black/[0.04]">模板 {{
						formatNum(templateChars)
					}} 字 / {{ formatNum(templateTokens) }} tk</span>
				<span class="text-[11px] text-macsub px-2 py-1 rounded-full bg-black/[0.04]">输出 {{
						formatNum(outputChars)
					}} 字 / {{ formatNum(outputTokens) }} tk</span>
				<el-button size="small" text type="danger" :icon="'Delete'" @click="removeCurrent">删除</el-button>
			</div>
		</div>
		
		<div class="flex-1 min-h-0 flex">
			<template v-if="editing">
				<section class="flex-[1.18] min-w-0 flex flex-col p-4 gap-3 border-r border-macborder">
					<div class="flex-1 min-h-0">
						<MdEditor v-model="editing.content" completion-mode="template"/>
					</div>
				</section>
				
				<aside class="flex-[0.92] min-w-[420px] max-w-[820px] flex flex-col bg-[#fbfbfd]">
					<div
						class="h-12 shrink-0 px-4 border-b border-macborder flex items-center justify-between bg-white/80 backdrop-blur">
						<div class="flex items-center gap-2">
							<div class="w-2 h-2 rounded-full"
							     :class="previewError ? 'bg-red-500' : previewResult ? 'bg-green-500' : 'bg-gray-300'"></div>
							<div>
								<div class="text-sm font-semibold">实时预览</div>
								<div class="text-[11px] text-macsub">未保存内容也参与渲染</div>
							</div>
						</div>
						<div class="flex items-center gap-2">
							<el-radio-group v-model="previewMode" size="small">
								<el-radio-button label="raw">原文</el-radio-button>
								<el-radio-button label="html">渲染</el-radio-button>
							</el-radio-group>
							<el-switch v-model="autoPreview" size="small" active-text="自动"/>
							<el-button size="small" :icon="'Setting'" @click="showParams = true" round>参数</el-button>
							<el-button size="small" type="primary" :icon="'VideoPlay'" @click="runPreview()"
							           :loading="previewLoading" round>渲染
							</el-button>
						</div>
					</div>
					
					<div class="grid grid-cols-3 gap-2 p-3 shrink-0 border-b border-macborder bg-white/55">
						<div class="mac-panel px-3 py-2">
							<div class="text-[10px] text-macsub">输出 tokens</div>
							<div class="text-base font-semibold">{{ formatNum(outputTokens) }}</div>
						</div>
						<div class="mac-panel px-3 py-2">
							<div class="text-[10px] text-macsub">膨胀比</div>
							<div class="text-base font-semibold">{{ previewRatio }}</div>
						</div>
						<div class="mac-panel px-3 py-2">
							<div class="text-[10px] text-macsub">渲染</div>
							<div class="text-base font-semibold">{{ previewMs || '—' }}ms</div>
						</div>
					</div>
					
					<div v-if="previewError"
					     class="mx-3 mt-3 p-3 rounded-xl border border-red-200 bg-red-50 text-red-700 text-xs whitespace-pre-wrap">
						{{ previewError }}
					</div>
					
					<div class="px-4 py-2 shrink-0 flex items-center justify-between text-[11px] text-macsub">
						<span>{{ previewAt ? `最后渲染 ${previewAt}` : '等待首次渲染' }}</span>
						<span>{{ formatNum(outputChars) }} chars / {{ formatNum(outputTokens) }} tokens</span>
					</div>
					
					<pre v-if="previewMode === 'raw'"
					     class="flex-1 min-h-0 overflow-auto m-0 px-4 pb-4 text-[12px] leading-relaxed whitespace-pre-wrap break-words font-mono text-mactext">{{
							previewResult || '右侧会显示当前模板 + 样例运行时参数 + 当前记忆库渲染出的完整系统提示词。'
						}}</pre>
					<div v-else
					     class="pm-md-preview flex-1 min-h-0 overflow-auto px-5 pb-5 text-[13px] leading-relaxed text-mactext"
					     v-html="renderedPreview || '<p class=&quot;text-macsub&quot;>等待渲染结果</p>'"></div>
				</aside>
			</template>
			
			<div v-else class="flex-1 flex items-center justify-center text-macsub text-sm">选择或新建一个模板</div>
		</div>
		
		<el-dialog v-model="showParams" title="预览样例运行时参数" width="760px">
			<div class="text-xs text-macsub mb-2">默认来自后端当前运行时 params；改完会自动刷新预览。</div>
			<el-input v-model="sampleParams" type="textarea" resize="none" class="template-param-input"/>
			<template #footer>
				<el-button @click="resetParamsFromRuntime">重置为当前运行时参数</el-button>
				<el-button @click="showParams = false">关闭</el-button>
				<el-button type="primary" @click="runPreview(); showParams = false">立即渲染</el-button>
			</template>
		</el-dialog>
		
		<el-dialog v-model="showHelp" title="模板语法说明" width="760px">
			<div class="text-sm space-y-3 leading-relaxed">
				<p class="text-macsub">模板用兼容 prompt-memory 的语法拼装系统提示词，不与 Markdown 冲突。模板页输入
					<code>[[</code> 会补变量/函数，输入 <code>@</code> 会补模板指令。</p>
				<div>
					<div class="font-semibold mb-1">变量插值</div>
					<pre class="bg-black/[0.04] p-2 rounded text-xs font-mono">[[ runtimeInfo.host ]]          运行时信息
[[ workspaceDir ]]             工作目录
[[ helpers.toolLines(builtinToolNames, builtinToolSummaries) ]]  内置工具清单
[[ helpers.toolLines(mcpToolNames, mcpToolSummaries) ]]  MCP 工具清单
[[ helpers.runtimeLine(runtimeInfo, defaultThinkLevel) ]]  Runtime 行
availableAgents / agents.available  当前可用 Agent 数组</pre>
				</div>
				<div>
					<div class="font-semibold mb-1">条件 / 循环 / 块</div>
					<pre class="bg-black/[0.04] p-2 rounded text-xs font-mono">@if helpers.has(toolNames,'gateway')
  ...内容...
@endif

@each e in memory.expandedEntries
## [[ e.title ]][[ helpers.noteSuffix(e.note) ]]
[[ e.body ]]
@endeach

@each item in mcpServerInstructions
## [[ item.server ]]
[[ item.instructions ]]
@endeach</pre>
				</div>
				<div>
					<div class="font-semibold mb-1">记忆数据(模板可用)</div>
					<pre class="bg-black/[0.04] p-2 rounded text-xs font-mono">memory.expandedEntries  每轮展开的完整记忆条目
memory.byCat.memory     长期记忆条目列表
memory.byCat.tools      工具说明条目列表
memory.groupsByCat.memory  长期记忆分组
memory.groupsByCat.tools   工具说明分组
memory.secretNames      凭证名称索引
memory.docNames         文档名称索引</pre>
				</div>
			</div>
		</el-dialog>
	</div>
</template>

<style>
.template-param-input .el-textarea__inner {
	height: 420px !important;
	font-family: "SF Mono", Menlo, Consolas, monospace;
	font-size: 12px;
	line-height: 1.55;
}

.pm-md-preview h1 {
	font-size: 1.35rem;
	font-weight: 700;
	margin: 1.1rem 0 .55rem;
}

.pm-md-preview h2 {
	font-size: 1.15rem;
	font-weight: 700;
	margin: 1rem 0 .45rem;
	padding-bottom: .25rem;
	border-bottom: 1px solid #e5e5ea;
}

.pm-md-preview h3 {
	font-size: 1rem;
	font-weight: 650;
	margin: .85rem 0 .35rem;
}

.pm-md-preview h4 {
	font-size: .92rem;
	font-weight: 650;
	margin: .7rem 0 .3rem;
}

.pm-md-preview p {
	margin: .32rem 0;
}

.pm-md-preview ul {
	margin: .35rem 0 .55rem 1.1rem;
	list-style: disc;
}

.pm-md-preview li {
	margin: .18rem 0;
}

.pm-md-preview code {
	font-family: "SF Mono", Menlo, Consolas, monospace;
	font-size: .88em;
	background: rgba(0, 0, 0, .055);
	border-radius: 5px;
	padding: 1px 4px;
}

.pm-md-preview pre {
	background: rgba(0, 0, 0, .045);
	border: 1px solid #e5e5ea;
	border-radius: 10px;
	padding: 10px;
	overflow: auto;
}

.pm-md-preview pre.hljs {
	background: #f6f8fa;
}

.pm-md-preview pre code {
	background: transparent;
	padding: 0;
}

.pm-md-preview table {
	width: 100%;
	border-collapse: collapse;
	margin: .7rem 0 1rem;
	font-size: .92em;
	overflow: hidden;
	border-radius: 10px;
}

.pm-md-preview th, .pm-md-preview td {
	border: 1px solid #dfe3ea;
	padding: 6px 9px;
	vertical-align: top;
}

.pm-md-preview th {
	background: #f6f8fa;
	font-weight: 650;
}

.pm-md-preview tr:nth-child(even) td {
	background: rgba(0, 0, 0, .018);
}

.pm-md-preview blockquote {
	margin: .6rem 0;
	padding: .35rem .8rem;
	border-left: 3px solid #b9c0cc;
	color: #4b5563;
	background: rgba(0, 0, 0, .025);
	border-radius: 0 8px 8px 0;
}

.pm-md-preview a {
	color: #0066cc;
	text-decoration: none;
}

.pm-md-preview .md-gap {
	height: .35rem;
}
</style>
