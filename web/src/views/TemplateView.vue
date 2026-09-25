<script setup>
import {computed, onBeforeUnmount, onMounted, ref, watch} from "vue";
import {ElMessage, ElMessageBox} from "element-plus";
import {encode} from "gpt-tokenizer";
import MarkdownIt from "markdown-it";
import hljs from "highlight.js";
import "highlight.js/styles/github.css";
import {Api, apiError} from "../api";
import MdEditor from "../components/AdaptiveMdEditor.vue";
import MobileAdminSummary from "../components/MobileAdminSummary.vue";

const templates = ref([]);
const activeId = ref(null);
const editing = ref(null);
const original = ref("");
const saving = ref(false);
const changingTemplate = ref(false);
let alive = true;
let loadRequest = 0;
const showHelp = ref(false);
const mobilePane = ref("edit");
const showParams = ref(false);
const showBuiltinImport = ref(false);
const builtinImportKinds = ref(["main", "agent"]);
const builtinImportLoading = ref(false);

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

async function load(keepId = activeId.value, {preserveDirty = false, selectEditor = true} = {}) {
	const request = ++loadRequest;
	const target = editing.value;
	const snapshot = JSON.stringify(target);
	const data = await Api.templates();
	if (!alive || request !== loadRequest) return;
	templates.value = data.items || [];
	if (data.promptParams) {
		runtimePromptParams.value = data.promptParams;
		if (!sampleParams.value || sampleParams.value === "{}" || sampleParamsNeedRuntimeRefresh()) resetParamsFromRuntime();
	}
	// List refreshes must never overwrite a different editor or a newer draft.
	if (!selectEditor || editing.value !== target || JSON.stringify(editing.value) !== snapshot) return;
	if (preserveDirty && dirty()) return;
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

async function confirmDiscard() {
	const target = editing.value;
	const snapshot = JSON.stringify(target);
	if (dirty()) {
		try {
			await ElMessageBox.confirm("当前模板有未保存修改，切换后会丢失这些修改。确定切换？", "切换模板", {
				type: "warning", confirmButtonText: "放弃并切换", cancelButtonText: "继续编辑",
			});
		} catch { return false; }
	}
	return alive && editing.value === target && JSON.stringify(editing.value) === snapshot;
}

async function selectById(id) {
	if (changingTemplate.value) { activeId.value = editing.value?.id || null; return; }
	changingTemplate.value = true;
	try {
		if (!await confirmDiscard()) { activeId.value = editing.value?.id || null; return; }
		const t = templates.value.find((x) => x.id === id);
		if (t) select(t);
	} finally { changingTemplate.value = false; }
}

onMounted(async () => {
	await load();
});

async function saveTemplate(patch = {}, message = "已保存") {
	if (!alive || !editing.value || saving.value || changingTemplate.value) return;
	const target = editing.value;
	const submitted = {...JSON.parse(JSON.stringify(target)), ...patch};
	saving.value = true;
	let committed = false;
	try {
		const r = await Api.updateTemplate(submitted.id, submitted);
		if (r?.ok === false) throw new Error(r.error || "保存失败");
		committed = true;
		if (!alive) return;
		const current = editing.value === target;
		if (current) {
			Object.assign(target, patch);
			original.value = JSON.stringify(submitted);
		}
		ElMessage.success(current && dirty() ? `${message}；后续修改仍未保存` : message);
		await load(submitted.id, {preserveDirty: true, selectEditor: current});
	} catch (error) {
		if (alive) {
			if (committed) ElMessage.warning(`已保存，但列表刷新失败：${apiError(error)}`);
			else ElMessage.error(`保存失败：${apiError(error)}`);
		}
	} finally { saving.value = false; }
}

function save() { return saveTemplate(); }
function activate() { return saveTemplate({is_active: 1}, "已设为激活模板"); }
function activateAgent() { return saveTemplate({is_agent_active: 1}, "已设为 Agent 提示词"); }

async function newTpl() {
	if (!alive || saving.value || changingTemplate.value) return;
	changingTemplate.value = true;
	let created = false;
	try {
		if (!await confirmDiscard()) return;
		const target = editing.value;
		const snapshot = JSON.stringify(target);
		const r = await Api.createTemplate({name: "新模板", content: SAMPLE, is_active: 0, is_agent_active: 0});
		if (r?.ok === false) throw new Error(r.error || "创建失败");
		created = true;
		if (!alive) return;
		await load(r.id, {selectEditor: editing.value === target && JSON.stringify(editing.value) === snapshot});
	} catch (error) {
		if (alive) {
			if (created) ElMessage.warning(`模板已创建，但列表刷新失败：${apiError(error)}`);
			else ElMessage.error(`创建失败：${apiError(error)}`);
		}
	} finally { changingTemplate.value = false; }
}

function openBuiltinImport() {
	builtinImportKinds.value = ["main", "agent"];
	showBuiltinImport.value = true;
}

async function importBuiltinTemplates() {
	if (!builtinImportKinds.value.length || builtinImportLoading.value) return;
	builtinImportLoading.value = true;
	let result;
	try {
		result = await Api.importBuiltinTemplates([...builtinImportKinds.value]);
		if (result?.ok === false) throw new Error(result.error || "导入失败");
	} catch (error) {
		const code = apiError(error);
		const messages = {
			builtin_template_kinds_required: "请至少选择一种模板",
			unsupported_builtin_template_kind: "所选模板类型不受支持",
			bundled_template_unavailable: "当前安装中的提示词文件不可用",
			bundled_template_validation_failed: "随版本模板未通过校验",
		};
		ElMessage.error(`导入失败：${messages[code] || code}`);
		return;
	} finally {
		builtinImportLoading.value = false;
	}

	showBuiltinImport.value = false;
	try {
		await load(activeId.value, {preserveDirty: true});
	} catch (error) {
		ElMessage.warning(`模板已导入，但列表刷新失败：${apiError(error)}`);
		return;
	}
	const created = Number(result?.created || 0);
	const reused = Number(result?.reused || 0);
	if (created) {
		const reusedText = reused ? `，另有 ${reused} 个已存在` : "";
		ElMessage.success(`已导入 ${created} 个未激活模板${reusedText}。导入不会自动激活，也不改变已有会话提示词。`);
	} else {
		ElMessage.info("所选随版本模板已存在，未新增重复副本。导入不会自动激活，也不改变已有会话提示词。");
	}
}

async function removeCurrent() {
	if (!editing.value || saving.value || changingTemplate.value) return;
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
	if (!alive || !autoPreview.value || !editing.value) return;
	clearTimeout(previewTimer);
	previewTimer = setTimeout(() => runPreview({silent: true}), delay);
}

onBeforeUnmount(() => { alive = false; loadRequest++; previewSeq++; clearTimeout(previewTimer); });

watch(() => editing.value?.content, () => schedulePreview(), {flush: "post"});
watch(sampleParams, () => schedulePreview(500));
watch(autoPreview, (v) => {
	if (v) schedulePreview(80);
});

const SAMPLE = `You are OpenBear, a capable AI assistant operating inside a private Web console.\n\nWorkspace: [[ workspaceDir ]]\n\n## Tools\n\n### Built-in tools\n[[ helpers.toolLines(builtinToolNames, builtinToolSummaries) ]]\n\n@if mcpToolNames\n### MCP tools\n[[ helpers.toolLines(mcpToolNames, mcpToolSummaries) ]]\n@endif\n\n@if mcpServerInstructions\n### MCP server instructions\n@each item in mcpServerInstructions\n#### [[ item.server ]]\n[[ item.instructions ]]\n@endeach\n@endif`;
</script>

<template>
	<div class="admin-page template-page h-full flex flex-col">
		<header
			class="h-14 shrink-0 flex items-center justify-between px-6 border-b border-macborder bg-ob-surface/70 backdrop-blur">
			<div class="admin-heading flex items-center gap-2 min-w-0">
				<h1 class="text-base font-semibold shrink-0">提示词模板</h1>
				<span class="text-xs text-macsub truncate">选择模板 · 编辑 · 自动补全 · 实时预览</span>
			</div>
			<div class="admin-desktop-only template-actions flex gap-2 shrink-0">
				<el-button :icon="'QuestionFilled'" @click="showHelp = true" round>语法说明</el-button>
				<el-button :icon="'Download'" @click="openBuiltinImport" round>导入随版本模板</el-button>
				<el-button :icon="'Plus'" @click="newTpl" round :disabled="saving || changingTemplate">新建</el-button>
				<el-button @click="save" round :disabled="saving || changingTemplate || !dirty()">保存</el-button>
				<el-button type="primary" @click="activate" round :disabled="saving || changingTemplate || !editing || editing.is_active">设为激活</el-button>
				<el-button type="success" @click="activateAgent" round :disabled="saving || changingTemplate || !editing || editing.is_agent_active">设为Agent提示词</el-button>
			</div>
			<div class="admin-mobile-only template-mobile-actions">
				<button type="button" :disabled="saving || changingTemplate" @click="newTpl">新建</button>
				<button type="button" class="is-primary" :disabled="saving || changingTemplate || !dirty()" @click="save">保存</button>
				<details class="template-more" @keydown.esc="$event.currentTarget.open = false">
					<summary>更多 <span aria-hidden="true">⌄</span></summary>
					<div class="template-more-menu" @click="$event.currentTarget.closest('details').open = false">
						<button type="button" :disabled="saving || changingTemplate || !editing || editing.is_active" @click="activate">设为主提示词</button>
						<button type="button" :disabled="saving || changingTemplate || !editing || editing.is_agent_active" @click="activateAgent">设为 Agent 提示词</button>
						<button type="button" @click="openBuiltinImport">导入随版本模板</button>
						<button type="button" @click="showHelp = true">语法说明</button>
						<button type="button" class="is-danger" :disabled="saving || changingTemplate || !editing" @click="removeCurrent">删除当前模板</button>
					</div>
				</details>
			</div>
		</header>
		
		<div v-if="editing" class="template-meta h-14 shrink-0 px-4 border-b border-macborder bg-ob-surface/55 flex items-center gap-3">
			<div class="flex items-center gap-2 min-w-0">
				<span class="text-xs text-macsub shrink-0">模板</span>
				<el-select v-model="activeId" :disabled="changingTemplate" @change="selectById" filterable class="!w-72" aria-label="选择模板" placeholder="选择模板">
					<el-option v-for="t in templates" :key="t.id"
					           :label="t.name + (t.is_active ? ' · 激活' : '') + (t.is_agent_active ? ' · Agent提示词' : '')"
					           :value="t.id"/>
				</el-select>
				<el-input v-model="editing.name" aria-label="模板名称" placeholder="模板名" class="!w-72"/>
			</div>
			
			<div class="template-status flex items-center gap-2 text-xs shrink-0">
				<span v-if="editing.is_active" class="text-ob-success">● 当前激活</span>
				<span v-else class="text-macsub">激活: {{ activeTemplateName }}</span>
				<span v-if="editing.is_agent_active" class="text-ob-success">● Agent提示词</span>
				<span v-else class="text-macsub">Agent: {{ agentActiveTemplateName }}</span>
				<span v-if="dirty()" class="text-ob-orange">● 未保存</span>
			</div>
			
			<div class="template-counters ml-auto flex items-center gap-2 shrink-0">
				<span class="text-[11px] text-macsub px-2 py-1 rounded-full bg-ob-soft">模板 {{
						formatNum(templateChars)
					}} 字 / {{ formatNum(templateTokens) }} tk</span>
				<span class="text-[11px] text-macsub px-2 py-1 rounded-full bg-ob-soft">输出 {{
						formatNum(outputChars)
					}} 字 / {{ formatNum(outputTokens) }} tk</span>
				<el-button size="small" text type="danger" :icon="'Delete'" @click="removeCurrent">删除</el-button>
			</div>
		</div>
		
		<MobileAdminSummary v-if="editing" :items="[{ label: '主提示词', value: activeTemplateName }, { label: 'Agent 提示词', value: agentActiveTemplateName }, { label: '模板 tokens', value: formatNum(templateTokens) }, { label: '输出 tokens', value: formatNum(outputTokens) }, { label: '膨胀比', value: previewRatio }, { label: '渲染耗时', value: previewMs + 'ms' }]">模板 {{ formatNum(templateChars) }} 字 · 输出 {{ formatNum(outputChars) }} 字 <span v-if="dirty()" class="text-ob-orange">· 未保存</span></MobileAdminSummary>
		<div v-if="editing" class="admin-mobile-only template-pane-switch" role="group" aria-label="模板工作区">
			<button type="button" :aria-pressed="mobilePane === 'edit'" aria-controls="template-editor-pane" @click="mobilePane = 'edit'">编辑模板</button>
			<button type="button" :aria-pressed="mobilePane === 'preview'" aria-controls="template-preview-pane" @click="mobilePane = 'preview'">实时预览</button>
		</div>
		<div class="template-workspace flex-1 min-h-0 flex" :class="`mobile-pane-${mobilePane}`">
			<template v-if="editing">
				<section id="template-editor-pane" class="template-editor-pane flex-[1.18] min-w-0 flex flex-col p-4 gap-3 border-r border-macborder">
					<div class="flex-1 min-h-0">
						<MdEditor v-model="editing.content" completion-mode="template"/>
					</div>
				</section>
				
				<aside id="template-preview-pane" class="template-preview-pane flex-[0.92] min-w-[420px] max-w-[820px] flex flex-col bg-ob-bg">
					<div
						class="template-preview-toolbar h-12 shrink-0 px-4 border-b border-macborder flex items-center justify-between bg-ob-surface/80 backdrop-blur">
						<div class="flex items-center gap-2">
							<div class="w-2 h-2 rounded-full"
							     :class="previewError ? 'bg-ob-danger' : previewResult ? 'bg-ob-success' : 'bg-ob-soft'"></div>
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
					
					<div class="admin-desktop-only grid grid-cols-3 gap-2 p-3 shrink-0 border-b border-macborder bg-ob-surface/55">
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
					     class="mx-3 mt-3 p-3 rounded-xl border border-ob-danger/25 bg-[var(--ob-danger-soft)] text-ob-danger text-xs whitespace-pre-wrap">
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
		
		<el-dialog append-to-body class="admin-dialog template-dialog"
			v-model="showBuiltinImport"
			title="导入随版本模板"
			width="520px"
			:close-on-click-modal="!builtinImportLoading"
			:close-on-press-escape="!builtinImportLoading"
		>
			<div class="text-sm leading-relaxed text-mactext">
				<p class="mb-3 text-macsub">从当前 OpenBear 安装包读取提示词，并保存为可自行检查、编辑和激活的模板副本。</p>
				<el-checkbox-group v-model="builtinImportKinds" class="flex flex-col gap-2">
					<el-checkbox value="main" border class="builtin-template-option">
						<span class="block font-medium text-mactext">主控提示词</span>
						<span class="mt-0.5 block text-xs font-normal text-macsub">当前版本附带的 OpenBear 主控模板</span>
					</el-checkbox>
					<el-checkbox value="agent" border class="builtin-template-option">
						<span class="block font-medium text-mactext">Agent 提示词</span>
						<span class="mt-0.5 block text-xs font-normal text-macsub">当前版本附带的 Agent 基础模板</span>
					</el-checkbox>
				</el-checkbox-group>
				<div class="mt-4 rounded-xl border border-ob-border bg-ob-soft px-3 py-2.5 text-xs text-ob-subtle">
					导入不会自动激活，也不改变已有会话提示词。相同版本内容已存在时不会重复创建。
				</div>
			</div>
			<template #footer>
				<el-button @click="showBuiltinImport = false" :disabled="builtinImportLoading">取消</el-button>
				<el-button
					type="primary"
					:loading="builtinImportLoading"
					:disabled="!builtinImportKinds.length"
					@click="importBuiltinTemplates"
				>导入所选模板</el-button>
			</template>
		</el-dialog>

		<el-dialog append-to-body class="admin-dialog template-dialog" v-model="showParams" title="预览样例运行时参数" width="760px">
			<div class="text-xs text-macsub mb-2">默认来自后端当前运行时 params；改完会自动刷新预览。</div>
			<el-input v-model="sampleParams" type="textarea" resize="none" class="template-param-input"/>
			<template #footer>
				<el-button @click="resetParamsFromRuntime">重置为当前运行时参数</el-button>
				<el-button @click="showParams = false">关闭</el-button>
				<el-button type="primary" @click="runPreview(); showParams = false">立即渲染</el-button>
			</template>
		</el-dialog>
		
		<el-dialog append-to-body class="admin-dialog template-dialog" v-model="showHelp" title="模板语法说明" width="760px">
			<div class="text-sm space-y-3 leading-relaxed">
				<p class="text-macsub">模板用兼容 prompt-memory 的语法拼装系统提示词，不与 Markdown 冲突。模板页输入
					<code>[[</code> 会补变量/函数，输入 <code>@</code> 会补模板指令。</p>
				<div>
					<div class="font-semibold mb-1">变量插值</div>
					<pre class="bg-ob-soft p-2 rounded text-xs font-mono">[[ runtimeInfo.host ]]          运行时信息
[[ workspaceDir ]]             工作目录
[[ helpers.toolLines(builtinToolNames, builtinToolSummaries) ]]  内置工具清单
[[ helpers.toolLines(mcpToolNames, mcpToolSummaries) ]]  MCP 工具清单（Agent 仅含本轮授权）
[[ helpers.runtimeLine(runtimeInfo, defaultThinkLevel) ]]  Runtime 行
availableAgents / agents.available  当前可用 Agent 数组</pre>
				</div>
				<div>
					<div class="font-semibold mb-1">条件 / 循环 / 块</div>
                    <p class="text-xs text-macsub">Agent 模板的 MCP 名单与服务说明仅来自授权工具，不包含未授权服务；服务说明不扩大权限。</p>
					<pre class="bg-ob-soft p-2 rounded text-xs font-mono">@if helpers.has(toolNames,'gateway')
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
					<pre class="bg-ob-soft p-2 rounded text-xs font-mono">memory.expandedEntries  每轮展开的完整记忆条目
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
.builtin-template-option.el-checkbox.is-bordered {
	width: 100%;
	height: auto;
	margin: 0;
	padding: 11px 14px;
	align-items: flex-start;
	border-radius: 12px;
}

.builtin-template-option .el-checkbox__input {
	margin-top: 2px;
}

.builtin-template-option .el-checkbox__label {
	line-height: 1.35;
}

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
	border-bottom: 1px solid var(--ob-border);
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
	background: rgb(var(--ob-border-rgb) / .055);
	border-radius: 5px;
	padding: 1px 4px;
}

.pm-md-preview pre {
	background: rgb(var(--ob-border-rgb) / .045);
	border: 1px solid var(--ob-border);
	border-radius: 10px;
	padding: 10px;
	overflow: auto;
}

.pm-md-preview pre.hljs {
	background: var(--ob-surface-soft);
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
	border: 1px solid var(--ob-border);
	padding: 6px 9px;
	vertical-align: top;
}

.pm-md-preview th {
	background: var(--ob-surface-soft);
	font-weight: 650;
}

.pm-md-preview tr:nth-child(even) td {
	background: rgb(var(--ob-border-rgb) / .018);
}

.pm-md-preview blockquote {
	margin: .6rem 0;
	padding: .35rem .8rem;
	border-left: 3px solid var(--ob-border-strong);
	color: var(--ob-text);
	background: rgb(var(--ob-border-rgb) / .025);
	border-radius: 0 8px 8px 0;
}

.pm-md-preview a {
	color: var(--ob-blue);
	text-decoration: none;
}

.pm-md-preview .md-gap {
	height: .35rem;
}

/* OpenBear system dark theme */
html.dark .pm-md-preview h2 {
		border-bottom: 1px solid var(--ob-border);
	}
html.dark .pm-md-preview code {
		background: rgb(var(--ob-surface-rgb) / 0.069);
	}
html.dark .pm-md-preview pre {
		background: rgb(var(--ob-surface-rgb) / 0.056);
		border: 1px solid var(--ob-border);
	}
html.dark .pm-md-preview pre.hljs {
		background: var(--ob-surface);
	}
html.dark .pm-md-preview th,
html.dark .pm-md-preview td {
		border: 1px solid var(--ob-border);
	}
html.dark .pm-md-preview th {
		background: var(--ob-surface);
	}
html.dark .pm-md-preview tr:nth-child(even) td {
		background: rgb(var(--ob-surface-rgb) / 0.035);
	}
html.dark .pm-md-preview blockquote {
		border-left: 3px solid var(--ob-border);
		color: var(--ob-text);
		background: rgb(var(--ob-surface-rgb) / 0.035);
	}
html.dark .pm-md-preview a {
		color: var(--ob-blue);
	}
</style>
