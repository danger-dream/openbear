<script setup>
import {computed} from "vue";
import {ArrowRight} from "@element-plus/icons-vue";
import ConsoleMarkdown from "./ConsoleMarkdown.vue";
import ToolArgumentsView from "./ToolArgumentsView.vue";
import {toolArgumentsSummary} from "./toolArgumentsPresentation.js";

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
	const description = String(line.toolDescription || line.description || "").trim()
		|| toolArgumentsSummary(name, rawArguments);
	const status = declaredStatus(line, TOOL_STATUS_LABELS);
	return {
		...line,
		processTool: {
			name,
			rawArguments,
			description,
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
				<div class="activity-compaction-body">
					<div v-if="line.compaction?.summaryId || line.compaction?.compactionId" class="activity-compaction-id">
						{{ line.compaction.summaryId || line.compaction.compactionId }}
					</div>
					<ConsoleMarkdown v-if="line.compactedOutput" class="activity-compaction-output" :text="line.compactedOutput"/>
					<p v-else-if="line.compaction?.failed" class="activity-compaction-empty">{{ line.compaction.reason || '未生成可用压缩摘要' }}</p>
					<p v-else class="activity-compaction-empty">{{ line.emptyOutputText || '旧记录未持久化压缩摘要' }}</p>
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
			</div>
			<details v-else-if="line.processTool" class="activity-tool-call">
				<summary>
					<strong class="activity-process-name activity-tool-name">{{ line.processTool.name }}</strong>
					<i v-if="line.processTool.description" class="activity-process-separator" aria-hidden="true">·</i>
					<span v-if="line.processTool.description" class="activity-tool-description" :title="line.processTool.description">{{ line.processTool.description }}</span>
					<i class="activity-process-separator" aria-hidden="true">·</i>
					<span class="activity-process-status activity-tool-status" :class="`tone-${line.processTool.statusTone}`">{{ line.processTool.status }}</span>
					<ArrowRight class="activity-tool-arrow"/>
				</summary>
				<div class="activity-tool-arguments">
					<ToolArgumentsView :tool-name="line.processTool.name" :raw-arguments="line.processTool.rawArguments" compact/>
				</div>
			</details>
			<p v-else>{{ line.message }}</p>
		</div>
	</div>
	<div v-else class="activity-list-empty">{{ emptyText }}</div>
</template>

<style scoped>
.activity-list { position: relative; min-width: 0; padding-left: 4px; }
.activity-list::before { content: ""; position: absolute; left: 62px; top: 6px; bottom: 6px; width: 1px; background: #e4e7ec; }
.activity-row { position: relative; display: grid; grid-template-columns: 48px 12px minmax(0, 1fr); gap: 7px; align-items: baseline; padding: 5px 0; }
.activity-row time { width: 48px; color: #a1a1aa; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; white-space: nowrap; }
.activity-dot { z-index: 1; width: 7px; height: 7px; border: 2px solid #fff; border-radius: 50%; background: #98a2b3; box-shadow: 0 0 0 1px #d0d5dd; }
.activity-row.is-control .activity-dot { background: #d97706; }
.activity-row.tone-active .activity-dot { background: #2563eb; box-shadow: 0 0 0 1px #93c5fd; }
.activity-row.tone-success .activity-dot { background: #16a34a; box-shadow: 0 0 0 1px #86efac; }
.activity-row.tone-danger .activity-dot { background: #dc2626; box-shadow: 0 0 0 1px #fca5a5; }
.activity-row p { min-width: 0; margin: 0; overflow: hidden; color: #52525b; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; word-break: break-word; }
.activity-row.tone-success p { color: #357047; }
.activity-row.tone-danger p { color: #b42318; }
.activity-row.is-tool-call, .activity-row.is-model-call { align-items: start; }
.activity-row.is-tool-call > time, .activity-row.is-model-call > time, .activity-row.is-compaction > time { padding-top: 3px; }
.activity-row.is-tool-call > .activity-dot, .activity-row.is-model-call > .activity-dot, .activity-row.is-compaction > .activity-dot { margin-top: 5px; }
.activity-tool-call, .activity-model-call { min-width: 0; grid-column: 3; }
.activity-tool-call { overflow: visible; }
.activity-tool-call > summary, .activity-model-call { display: flex; max-width: 100%; min-width: 0; align-items: center; gap: 6px; overflow: hidden; padding: 0; color: #52525b; font-size: 12px; line-height: 1.5; }
.activity-tool-call > summary { cursor: pointer; list-style: none; }
.activity-tool-call > summary::-webkit-details-marker { display: none; }
.activity-process-name { min-width: max-content; flex: 0 0 auto; overflow: visible; color: #52525b; font-weight: 620; text-overflow: clip; white-space: nowrap; }
.activity-process-separator { flex: 0 0 auto; color: #c4c4ca; font-style: normal; }
.activity-tool-description, .activity-model-description { min-width: 0; flex: 0 1 auto; overflow: hidden; color: #8a8a93; text-overflow: ellipsis; white-space: nowrap; }
.activity-model-meta { min-width: max-content; flex: 0 0 auto; color: #71717a; white-space: nowrap; }
.activity-process-status { min-width: max-content; flex: 0 0 auto; font-size: 11px; font-weight: 600; white-space: nowrap; }
.activity-process-status.tone-active { color: #2563eb; }
.activity-process-status.tone-success { color: #357047; }
.activity-process-status.tone-danger { color: #b42318; }
.activity-tool-arrow { width: .72rem; height: .72rem; flex: 0 0 auto; color: #a1a1aa; transition: transform .14s ease; }
.activity-tool-call[open] > summary .activity-tool-arrow { transform: rotate(90deg); }
.activity-tool-call > summary:hover .activity-tool-name, .activity-tool-call > summary:focus-visible .activity-tool-name { color: #27272a; }
.activity-tool-call > summary:focus-visible { border-radius: 4px; outline: 2px solid rgba(67,56,202,.16); outline-offset: 2px; }
.activity-tool-arguments { min-width: 0; margin: 7px 0 4px; border-left: 1px solid #e4e4e7; padding-left: 9px; }
.activity-compaction { min-width: 0; overflow: hidden; }
.activity-compaction > summary { display: flex; min-width: 0; align-items: center; gap: 5px; overflow: hidden; padding: 0; color: #357047; cursor: pointer; font-size: 12px; line-height: 1.5; list-style: none; }
.activity-row.tone-danger .activity-compaction > summary { color: #b42318; }
.activity-compaction > summary::-webkit-details-marker { display: none; }
.activity-compaction > summary span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.activity-compaction > summary svg { width: 10px; flex: 0 0 auto; color: #a1a1aa; transition: transform .14s ease; }
.activity-compaction[open] > summary svg { transform: rotate(90deg); }
.activity-compaction-body { max-height: 360px; overflow: auto; margin: 7px 0 4px; border-left: 1px solid #dfe8e1; padding: 2px 0 2px 9px; }
.activity-compaction-id { margin-bottom: 5px; color: #a1a1aa; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 10px; overflow-wrap: anywhere; }
.activity-compaction-empty { color: #8a8a93 !important; }
.activity-list-empty { border: 1px dashed #d4d4d8; border-radius: 9px; padding: 13px; color: #a1a1aa; font-size: 11px; text-align: center; }
.compact .activity-row { padding: 4px 0; }
.compact .activity-row time { font-size: 10.5px; }
.compact .activity-row p, .compact .activity-tool-call > summary, .compact .activity-model-call { font-size: 11.5px; }

@media (prefers-reduced-motion: reduce) {
	.activity-tool-arrow, .activity-compaction > summary svg { transition: none; }
}
</style>
