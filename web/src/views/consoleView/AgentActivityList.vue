<script setup>
import {computed} from "vue";
import {ArrowRight} from "@element-plus/icons-vue";
import ConsoleMarkdown from "./ConsoleMarkdown.vue";
import ToolArgumentsView from "./ToolArgumentsView.vue";
import {toolArgumentsSummary} from "./toolArgumentsPresentation.js";
import {toolDisplayLabel} from "./toolCallPresentation.js";

const props = defineProps({
	lines: {type: Array, default: () => []},
	emptyText: {type: String, default: "暂无过程记录。"},
	compact: {type: Boolean, default: false},
});

const TOOL_STATUS_LABELS = {
	running: "执行中",
	success: "执行完成 √",
	failed: "执行失败 ×",
	denied: "已拒绝 ×",
};

const MODEL_STATUS_LABELS = {
	running: "调用中",
	success: "执行完成 √",
	failed: "调用失败 ×",
};

const STATUS_TONES = {
	running: "active",
	success: "success",
	failed: "danger",
	denied: "danger",
};

function isModelProcessLine(line = {}) {
	return line.processType === "model" || line.kind === "model_call_compact";
}

function isToolProcessLine(line = {}) {
	if (line.kind === "context_compaction_compact" || isModelProcessLine(line)) return false;
	if (line.processStatus) return true;
	return ["tool_call_started", "tool_call_finished", "tool_call_failed", "tool_call_denied", "tool_call_compact"].includes(line.kind);
}

function declaredStatus(line = {}, allowed = {}) {
	const declared = String(line.processStatus || line.modelStatus || "").trim().toLowerCase();
	if (allowed[declared]) return declared;
	if (line.kind === "tool_call_denied") return "denied";
	if (line.kind === "tool_call_failed" || line.tone === "danger") return "failed";
	if (line.tone === "success" || line.kind === "tool_call_finished") return "success";
	return "running";
}

function recordedText(value) {
	if (value === null || value === undefined) return "";
	return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

const presentedLines = computed(() => props.lines.map((line) => {
	const detail = line?.detail && typeof line.detail === "object" ? line.detail : {};
	if (isModelProcessLine(line)) {
		const status = declaredStatus(line, MODEL_STATUS_LABELS);
		return {
			...line,
			processModel: {
				label: String(line.modelLabel || detail.modelLabel || detail.model || "模型"),
				thinkLevel: String(line.modelThinkLevel || detail.thinkLevel || detail.think_level || "").trim(),
				fastMode: Boolean(line.modelFastMode),
				description: String(line.modelDescription || "").trim(),
				status: String(line.modelStatusText || MODEL_STATUS_LABELS[status]),
				statusTone: STATUS_TONES[status],
			},
		};
	}
	if (!isToolProcessLine(line)) return line;
	const name = String(line.toolName || detail.name || "Tool");
	const rawArguments = line.rawArguments ?? detail.arguments ?? "";
	const description = toolArgumentsSummary(name, rawArguments)
		|| String(line.toolDescription || line.description || "").trim();
	const status = declaredStatus(line, TOOL_STATUS_LABELS);
	return {
		...line,
		processTool: {
			name,
			label: toolDisplayLabel(name),
			rawArguments,
			description,
			// Only show fields actually supplied by the event. A preview is not a full result.
			result: recordedText(detail.resultText ?? detail.result ?? detail.resultPreview ?? detail.result_preview),
			resultIsPreview: detail.resultText == null && detail.result == null,
			fullDescription: String(line.description || detail.description || line.toolDescription || ""),
			failure: recordedText(detail.error ?? detail.reason),
			status: TOOL_STATUS_LABELS[status],
			statusTone: STATUS_TONES[status],
		},
	};
}));
</script>

<template>
	<div v-if="props.lines.length" class="activity-list" :class="{compact}">
		<div
			v-for="line in presentedLines"
			:key="line.key"
			class="activity-row"
			:class="[{'is-control': line.kind === 'agent_control', 'is-tool-call': Boolean(line.processTool), 'is-model-call': Boolean(line.processModel), 'is-compaction': line.kind === 'context_compaction_compact'}, line.tone ? `tone-${line.tone}` : '']"
		>
			<time>{{ line.timeLabel }}</time>
			<span class="activity-dot"></span>
			<details v-if="line.kind === 'context_compaction_compact'" class="activity-compaction">
				<summary><span>{{ line.message }}</span><ArrowRight/></summary>
				<div class="activity-compaction-body" tabindex="0" aria-label="上下文压缩详情，可滚动">
					<div v-if="line.compaction?.summaryId || line.compaction?.compactionId" class="activity-compaction-id">
						{{ line.compaction.summaryId || line.compaction.compactionId }}
					</div>
					<ul v-if="line.compaction?.detailFacts?.length" class="activity-compaction-facts">
						<li v-for="fact in line.compaction.detailFacts" :key="fact">{{ fact }}</li>
					</ul>
					<template v-if="line.compaction?.strategy !== 'sliding_window'">
						<ConsoleMarkdown v-if="line.compactedOutput" class="activity-compaction-output" :text="line.compactedOutput"/>
						<p v-else-if="line.compaction?.failed" class="activity-compaction-empty">{{ line.compaction.reason || '未生成可用压缩摘要' }}</p>
						<p v-else class="activity-compaction-empty">{{ line.emptyOutputText || '旧记录未持久化压缩摘要' }}</p>
					</template>
				</div>
			</details>
			<div v-else-if="line.processModel" class="activity-model-call">
				<strong class="activity-process-name activity-model-name">{{ line.processModel.label }}</strong>
				<template v-if="line.processModel.thinkLevel">
					<i class="activity-process-separator" aria-hidden="true">·</i>
					<span class="activity-model-meta">{{ line.processModel.thinkLevel }}</span>
				</template>
				<template v-if="line.processModel.fastMode">
					<i class="activity-process-separator" aria-hidden="true">·</i>
					<span class="activity-model-meta">Fast</span>
				</template>
				<template v-if="line.processModel.description">
					<i class="activity-process-separator" aria-hidden="true">·</i>
					<span class="activity-model-description" :title="line.processModel.description">{{ line.processModel.description }}</span>
				</template>
				<i class="activity-process-separator" aria-hidden="true">·</i>
				<span class="activity-process-status" :class="`tone-${line.processModel.statusTone}`">{{ line.processModel.status }}</span>
				<p v-if="line.detail?.error || line.detail?.reason" class="activity-failure">{{ recordedText(line.detail.error ?? line.detail.reason) }}</p>
			</div>
			<details v-else-if="line.processTool" class="activity-tool-call">
				<summary>
					<strong class="activity-process-name activity-tool-name">{{ line.processTool.label }}</strong>
					<i v-if="line.processTool.description" class="activity-process-separator" aria-hidden="true">·</i>
					<span v-if="line.processTool.description" class="activity-tool-description" :title="line.processTool.description">{{ line.processTool.description }}</span>
					<i class="activity-process-separator" aria-hidden="true">·</i>
					<span class="activity-process-status activity-tool-status" :class="`tone-${line.processTool.statusTone}`">{{ line.processTool.status }}</span>
					<ArrowRight class="activity-tool-arrow"/>
				</summary>
				<div class="activity-tool-arguments">
					<p v-if="line.processTool.fullDescription" class="activity-tool-full-description" :class="{'is-summary-copy': line.processTool.fullDescription === line.processTool.description}">{{ line.processTool.fullDescription }}</p>
					<p v-if="line.processTool.failure" class="activity-failure">{{ line.processTool.failure }}</p>
					<ToolArgumentsView :tool-name="line.processTool.name" :raw-arguments="line.processTool.rawArguments" compact/>
					<div v-if="line.processTool.result" class="activity-tool-result">
						<strong>{{ line.processTool.resultIsPreview ? '结果（已有预览）' : '结果' }}</strong>
						<pre tabindex="0" aria-label="已记录工具结果，可滚动">{{ line.processTool.result }}</pre>
					</div>
				</div>
			</details>
			<div v-else class="activity-message">
				<p>{{ line.message }}</p>
				<p v-if="line.kind === 'agent_control' && (line.detail?.text || line.detail?.message)" class="activity-control-text">{{ line.detail.text || line.detail.message }}</p>
			</div>
		</div>
	</div>
	<div v-else class="activity-list-empty">{{ emptyText }}</div>
</template>

<style scoped>
.activity-list { position: relative; min-width: 0; padding-left: 4px; }
.activity-list::before { content: ""; position: absolute; left: 62px; top: 6px; bottom: 6px; width: 1px; background: var(--ob-surface-soft); }
.activity-row { position: relative; display: grid; grid-template-columns: 48px 12px minmax(0, 1fr); gap: 7px; align-items: baseline; padding: 5px 0; }
.activity-row time { width: 48px; color: var(--ob-text-muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; white-space: nowrap; }
.activity-dot { z-index: 1; width: 7px; height: 7px; border: 2px solid var(--ob-border); border-radius: 50%; background: var(--ob-text-muted); box-shadow: 0 0 0 1px rgb(var(--ob-border-rgb) / 0.05); }
.activity-row.is-control .activity-dot { background: var(--ob-warning); }
.activity-row.tone-active .activity-dot { background: var(--ob-blue); box-shadow: 0 0 0 1px var(--ob-blue); }
.activity-row.tone-success .activity-dot { background: var(--ob-success); box-shadow: 0 0 0 1px var(--ob-success); }
.activity-row.tone-danger .activity-dot { background: var(--ob-danger); box-shadow: 0 0 0 1px var(--ob-danger); }
.activity-row p { min-width: 0; margin: 0; overflow: hidden; color: var(--ob-text); font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; word-break: break-word; }
.activity-row.tone-success p { color: var(--ob-success); }
.activity-row.tone-danger p { color: var(--ob-danger); }
.activity-row.is-tool-call, .activity-row.is-model-call { align-items: start; }
.activity-row.is-tool-call > time, .activity-row.is-model-call > time, .activity-row.is-compaction > time { padding-top: 3px; }
.activity-row.is-tool-call > .activity-dot, .activity-row.is-model-call > .activity-dot, .activity-row.is-compaction > .activity-dot { margin-top: 5px; }
.activity-tool-call, .activity-model-call { min-width: 0; grid-column: 3; }
.activity-tool-call { overflow: visible; }
.activity-tool-call > summary, .activity-model-call { display: flex; max-width: 100%; min-width: 0; align-items: center; gap: 6px; overflow: hidden; padding: 0; color: var(--ob-text-subtle); font-size: 12px; line-height: 1.5; }
.activity-tool-call > summary { cursor: pointer; list-style: none; }
.activity-tool-call > summary::-webkit-details-marker { display: none; }
.activity-process-name { min-width: max-content; flex: 0 0 auto; overflow: visible; color: var(--ob-text); font-weight: 620; text-overflow: clip; white-space: nowrap; }
.activity-process-separator { flex: 0 0 auto; color: var(--ob-text-muted); font-style: normal; }
.activity-tool-description { min-width: 0; flex: 0 1 auto; overflow: hidden; color: var(--ob-text-muted); text-overflow: ellipsis; white-space: nowrap; }
.activity-model-description { min-width: 0; flex: 0 1 auto; color: var(--ob-text-muted); white-space: normal; overflow-wrap: anywhere; }
.activity-model-call, .activity-tool-call[open] > summary { flex-wrap: wrap; overflow: visible; }
.activity-tool-call[open] > summary .activity-tool-description { white-space: normal; overflow-wrap: anywhere; overflow: visible; }
.activity-process-name { min-width: 0; max-width: 100%; flex-shrink: 1; overflow-wrap: anywhere; white-space: normal; }
.activity-message { min-width: 0; }
.activity-failure { flex-basis: 100%; white-space: pre-wrap; }
.activity-control-text, .activity-tool-full-description { margin-bottom: 6px; white-space: pre-wrap; }
.activity-compaction[open] > summary span { white-space: normal; overflow-wrap: anywhere; }
.activity-tool-result { min-width: 0; margin-top: 8px; }
.activity-tool-result > strong { font-size: 10px; color: var(--ob-text-subtle); }
.activity-tool-result pre { box-sizing: border-box; max-width: 100%; max-height: 220px; overflow: auto; margin: 4px 0 0; border: 1px solid var(--ob-border); border-radius: 7px; padding: 8px; color: inherit; white-space: pre-wrap; overflow-wrap: anywhere; scrollbar-width: thin; }
.activity-model-meta { min-width: max-content; flex: 0 0 auto; color: var(--ob-text-subtle); white-space: nowrap; }
.activity-process-status { min-width: 0; max-width: 100%; flex: 0 0 auto; font-size: 11px; font-weight: 600; white-space: normal; overflow-wrap: anywhere; }
.activity-process-status.tone-active { color: var(--ob-blue); }
.activity-process-status.tone-success { color: var(--ob-success); }
.activity-process-status.tone-danger { color: var(--ob-danger); }
.activity-tool-arrow { width: .72rem; height: .72rem; flex: 0 0 auto; color: var(--ob-text-muted); transition: transform .14s ease; }
.activity-tool-call[open] > summary .activity-tool-arrow { transform: rotate(90deg); }
.activity-tool-call > summary:hover .activity-tool-name, .activity-tool-call > summary:focus-visible .activity-tool-name { color: var(--ob-text); }
.activity-tool-call > summary:focus-visible { border-radius: 4px; outline: 2px solid rgb(var(--ob-violet-rgb) / 0.16); outline-offset: 2px; }
.activity-tool-arguments { min-width: 0; margin: 7px 0 4px; border-left: 1px solid var(--ob-border); padding-left: 9px; }
.activity-compaction { min-width: 0; overflow: hidden; }
.activity-compaction > summary { display: flex; min-width: 0; align-items: center; gap: 5px; overflow: hidden; padding: 0; color: var(--ob-success); cursor: pointer; font-size: 14px; line-height: 1.5; list-style: none; }
.activity-row.tone-danger .activity-compaction > summary { color: var(--ob-danger); }
.activity-compaction > summary::-webkit-details-marker { display: none; }
.activity-compaction > summary span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.activity-compaction > summary svg { width: 10px; flex: 0 0 auto; color: var(--ob-text-muted); transition: transform .14s ease; }
.activity-compaction[open] > summary svg { transform: rotate(90deg); }
.activity-compaction-body { max-height: 360px; overflow: auto; margin: 7px 0 4px; border-left: 1px solid var(--ob-border); padding: 2px 0 2px 9px; }
.activity-compaction-id { margin-bottom: 5px; color: var(--ob-text-muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; overflow-wrap: anywhere; }
.activity-compaction-facts { margin: 0 0 9px; padding: 0; list-style: none; color: var(--ob-text-subtle); font-size: 13px; line-height: 1.7; }
.activity-compaction-output :deep(p) { font-size: 14px; }
.activity-compaction .activity-compaction-empty { font-size: 13px; }
.activity-compaction-empty { color: var(--ob-text-muted) !important; }
.activity-list-empty { border: 1px dashed var(--ob-border); border-radius: 9px; padding: 13px; color: var(--ob-text-muted); font-size: 11px; text-align: center; }
.compact .activity-row { padding: 4px 0; }
.compact .activity-row time { font-size: 10.5px; }
.compact .activity-row p, .compact .activity-tool-call > summary, .compact .activity-model-call { font-size: 11.5px; }

@media (max-width: 760px), (hover: none) and (pointer: coarse) {
	/* Put time above the content, rather than sacrificing 64px of every line
	   to a desktop rail inside an already narrow work drawer. */
	.activity-row { grid-template-columns: 8px minmax(0, 1fr); gap: 2px 8px; padding: 7px 0; }
	.activity-list::before { left: 7px; }
	.activity-row time { grid-column: 2; grid-row: 1; width: auto; }
	.activity-dot { grid-column: 1; grid-row: 1; margin-top: 4px; }
	.activity-tool-call, .activity-model-call, .activity-compaction, .activity-message { grid-column: 2; grid-row: 2; }
	.activity-tool-call > summary { flex-wrap: wrap; align-items: baseline; gap: 3px 6px; min-height: 36px; }
	.activity-tool-description { flex: 1 1 100%; order: 1; white-space: normal; overflow: visible; overflow-wrap: anywhere; color: var(--el-text-color-regular); }
	.activity-tool-call > summary .activity-tool-arrow { margin-left: auto; }
	.activity-tool-call > summary > .activity-process-separator { display: none; }
	.activity-compaction > summary { min-height: 36px; }
	.activity-compaction > summary span { white-space: normal; overflow: visible; overflow-wrap: anywhere; }
	.activity-tool-arguments { padding-left: 0; border: 0; }
	.activity-compaction-body { box-sizing: border-box; border: 1px solid var(--el-border-color-lighter); border-radius: 9px; padding: 8px; }
	.activity-tool-full-description.is-summary-copy { display: none; } /* Hide only an exact copy already fully visible above. */
	.activity-compaction-body, .activity-tool-result pre { max-height: min(240px, calc(var(--mobile-viewport-height, 100dvh) * .35)); -webkit-overflow-scrolling: touch; }
}

@media (prefers-reduced-motion: reduce) {
	.activity-tool-arrow, .activity-compaction > summary svg { transition: none; }
}
</style>

<style>
/* OpenBear system dark theme */
html.dark .activity-tool-call > summary:focus-visible {
		outline: 2px solid rgb(var(--ob-blue-rgb) / 0.16);
	}
html.dark .activity-tool-result pre { border-color: var(--ob-border); }
</style>
