<script setup>
import {computed, onBeforeUnmount, ref, watch} from "vue";
import {ArrowRight} from "@element-plus/icons-vue";
import {Api, apiError} from "../../api.js";
import {copyTextToClipboard} from "../../utils/clipboard.js";
import {contextCompactionView} from "./agentPlanPresentation.js";
import {toolArgumentsRawPayload} from "./toolArgumentsPresentation.js";
import {toolDisplayState, toolResultText} from "./display.js";
import {highlightCodeHtml} from "./markdown.js";
import {useToolDetailCache} from "./toolDetailCache.js";

const props = defineProps({
	event: {type: Object, required: true},
	conversationUuid: {type: String, default: ""},
	open: {type: Boolean, default: false},
	activeIndex: {type: Number, default: 0},
});

const emit = defineEmits(["toggle", "select-tab"]);
const toolDetailCache = useToolDetailCache();

const copiedPayload = ref("");
let copiedPayloadTimer = 0;
const loadedToolDetail = ref(null);
const toolDetailLoading = ref(false);
const toolDetailError = ref("");
const loadedCompaction = ref(null);
const loadedCompactionSummaryId = ref("");
const compactionLoading = ref(false);
const compactionError = ref("");

function object(value) {
	return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function hasOwn(value, key) {
	return Object.prototype.hasOwnProperty.call(object(value), key);
}

function parseObject(value) {
	if (value && typeof value === "object" && !Array.isArray(value)) return value;
	try {
		const parsed = JSON.parse(String(value || ""));
		return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
	} catch {
		return {};
	}
}

function payloadText(value) {
	if (value === null || value === undefined) return "";
	if (typeof value === "string") return value;
	try {
		return JSON.stringify(value, null, 2);
	} catch {
		return String(value);
	}
}

function hasFullToolDetail(operationValue) {
	const payload = object(operationValue?.payload);
	// Lazy summary snapshots now reuse the card's established `arguments` input
	// for their bounded preview.  `detailAvailable` distinguishes that preview
	// from a real full payload, so expanding still requests the complete record.
	if (operationValue?.detailAvailable) return Boolean(operationValue?.detailLoaded);
	return Boolean(operationValue?.detailLoaded)
		|| ["args", "arguments", "result", "resultText"].some((key) => hasOwn(payload, key));
}

const operation = computed(() => object(props.event?.operation));
const operationId = computed(() => String(operation.value.opId || "").trim());
const operationRevision = computed(() => Number(operation.value.revision || 0) || 0);
const loadedDetailMatchesOperation = computed(() => {
	const detail = object(loadedToolDetail.value);
	return Boolean(
		operationId.value
		&& String(detail.opId || "") === operationId.value
		&& Number(detail.revision || 0) >= operationRevision.value,
	);
});
const hasLoadedToolDetail = computed(() => hasFullToolDetail(operation.value) || loadedDetailMatchesOperation.value);
const detailAvailable = computed(() => Boolean(operationId.value && operation.value.detailAvailable));

const displayEvent = computed(() => {
	if (!loadedDetailMatchesOperation.value) return props.event;
	const detail = object(loadedToolDetail.value);
	const currentOperation = operation.value;
	const payload = {...object(currentOperation.payload), ...object(detail.payload)};
	const originalCalls = Array.isArray(props.event?.calls) ? props.event.calls : [];
	const originalCall = object(originalCalls[0]);
	const call = {
		...originalCall,
		id: String(payload.toolCallId || originalCall.id || ""),
		name: String(payload.name || payload.toolName || originalCall.name || "Tool"),
		arguments: String(payload.arguments ?? payload.args ?? originalCall.arguments ?? ""),
		preview: String(payload.preview || originalCall.preview || ""),
	};
	const calls = originalCalls.length
		? originalCalls.map((item, index) => index === 0 ? call : item)
		: [call];
	const hasResult = hasOwn(payload, "resultText") || hasOwn(payload, "result");
	const rawResult = hasOwn(payload, "resultText") ? payload.resultText : payload.result;
	const result = hasResult ? {
		...object(props.event?.result),
		id: props.event?.result?.id || `op-result-${operationId.value}`,
		role: "tool",
		toolCallId: call.id,
		tool_call_id: call.id,
		name: call.name,
		content: payloadText(rawResult),
		durationMs: Number(payload.durationMs || 0),
	} : (props.event?.result || null);
	return {
		...props.event,
		calls,
		result,
		results: result ? [result] : [],
		operation: {...currentOperation, ...detail, payload},
	};
});
const toolState = computed(() => toolDisplayState(displayEvent.value, props.activeIndex, {includeDetail: props.open}));

const baseCompaction = computed(() => {
	const payload = object(displayEvent.value?.operation?.payload);
	const argumentsValue = toolState.value.activeArguments || payload.arguments || payload.args || "";
	return contextCompactionView({
		...payload,
		...parseObject(argumentsValue),
		name: toolState.value.activeToolName || payload.name || toolState.value.title,
		arguments: argumentsValue,
	});
});
const isContextCompaction = computed(() => baseCompaction.value.isCompaction && baseCompaction.value.scope !== "agent");
const rawCompactionResult = computed(() => toolState.value.activeResult
	? String(toolResultText(displayEvent.value, toolState.value.activeResult) || "").trim()
	: "");
const resultIsPreviewOnly = computed(() => Boolean(
	baseCompaction.value.summaryRef
	&& baseCompaction.value.outputPreview
	&& rawCompactionResult.value === baseCompaction.value.outputPreview,
));
const embeddedCompactionOutput = computed(() => baseCompaction.value.output
	|| (rawCompactionResult.value && !resultIsPreviewOnly.value ? rawCompactionResult.value : ""));
const resolvedCompaction = computed(() => contextCompactionView({
	...baseCompaction.value,
	...(loadedCompaction.value || {}),
	name: "ContextCompaction",
}));
const compactionOutput = computed(() => String(loadedCompaction.value?.compactedOutput || embeddedCompactionOutput.value || ""));
const compactionPreview = computed(() => baseCompaction.value.outputPreview
	|| (resultIsPreviewOnly.value ? rawCompactionResult.value : ""));

function formatPayload(value) {
	const raw = String(value ?? "").trim();
	if (!raw) return {content: "", language: ""};
	try {
		return {content: JSON.stringify(JSON.parse(raw), null, 2), language: "json"};
	} catch {
		return {content: raw, language: ""};
	}
}

const argumentsPayload = computed(() => toolArgumentsRawPayload(
	toolState.value.activeToolName,
	toolState.value.activeArguments,
));
const argumentsHtml = computed(() => highlightCodeHtml(argumentsPayload.value.content, argumentsPayload.value.language));
const resultSource = computed(() => isContextCompaction.value
	? (compactionOutput.value || compactionPreview.value || "")
	: (toolState.value.activeResult ? toolResultText(displayEvent.value, toolState.value.activeResult) : ""));
const resultPayload = computed(() => formatPayload(resultSource.value));
const resultHtml = computed(() => highlightCodeHtml(resultPayload.value.content, resultPayload.value.language));
const toolDetailNotice = computed(() => {
	if (isContextCompaction.value) return "";
	if (toolDetailLoading.value) return "正在读取完整工具详情…";
	return toolDetailError.value ? `工具详情读取失败：${toolDetailError.value}` : "";
});
const toolDetailNoticeIsError = computed(() => Boolean(!isContextCompaction.value && toolDetailError.value));
const resultNotice = computed(() => {
	if (isContextCompaction.value) {
		if (compactionLoading.value) return "正在读取完整压缩摘要…";
		if (compactionError.value) return `完整摘要读取失败：${compactionError.value}`;
		return resolvedCompaction.value.emptyOutputText;
	}
	if (toolDetailLoading.value) return "正在读取完整工具详情…";
	if (toolDetailError.value) return `工具详情读取失败：${toolDetailError.value}`;
	return "等待工具结果…";
});
const resultNoticeIsError = computed(() => Boolean(
	(isContextCompaction.value && compactionError.value)
	|| (!isContextCompaction.value && toolDetailError.value),
));

function payloadMeta(payload) {
	const content = String(payload?.content || "");
	if (!content) return "";
	return `${payload?.language === "json" ? "JSON" : "文本"} · ${content.length.toLocaleString()} 字符`;
}

async function copyPayload(payload, key) {
	if (!payload?.content) return;
	try {
		await copyTextToClipboard(payload.content);
		copiedPayload.value = key;
		if (copiedPayloadTimer) window.clearTimeout(copiedPayloadTimer);
		copiedPayloadTimer = window.setTimeout(() => { copiedPayload.value = ""; }, 1400);
	} catch {
		copiedPayload.value = "";
	}
}

onBeforeUnmount(() => {
	if (copiedPayloadTimer) window.clearTimeout(copiedPayloadTimer);
});

async function loadToolDetail() {
	const operationIdValue = operationId.value;
	const requestedRevision = operationRevision.value;
	const conversationUuid = String(props.conversationUuid || "").trim();
	if (
		!props.open
		|| !detailAvailable.value
		|| hasLoadedToolDetail.value
		|| isContextCompaction.value
		|| !conversationUuid
		|| conversationUuid.startsWith("local:")
		|| toolDetailLoading.value
	) return;
	toolDetailLoading.value = true;
	toolDetailError.value = "";
	let retryForNewRevision = false;
	try {
		const requestDetail = async () => {
			const data = await Api.conversationOperationDetail(conversationUuid, operationIdValue);
			if (data?.ok === false) throw new Error(data.error || "工具详情读取失败");
			return object(data?.operation);
		};
		const detail = toolDetailCache
			? await toolDetailCache.load({
				conversationUuid,
				operationId: operationIdValue,
				revision: requestedRevision,
				loader: requestDetail,
			})
			: await requestDetail();
		if (operationId.value !== operationIdValue || String(detail.opId || "") !== operationIdValue) return;
		if (Number(detail.revision || 0) < operationRevision.value || Number(detail.revision || 0) < requestedRevision) {
			retryForNewRevision = true;
			return;
		}
		loadedToolDetail.value = detail;
	} catch (error) {
		toolDetailError.value = apiError(error);
	} finally {
		toolDetailLoading.value = false;
		if (retryForNewRevision && props.open) void loadToolDetail();
	}
}

async function loadCompactionOutput() {
	const summaryId = String(baseCompaction.value.summaryId || "").trim();
	const conversationUuid = String(props.conversationUuid || "").trim();
	if (!props.open || !isContextCompaction.value || embeddedCompactionOutput.value || !/^\d+$/.test(summaryId)) return;
	if (!conversationUuid || conversationUuid.startsWith("local:") || compactionLoading.value) return;
	if (loadedCompactionSummaryId.value === summaryId && loadedCompaction.value) return;
	compactionLoading.value = true;
	compactionError.value = "";
	try {
		const data = await Api.conversationCompaction(conversationUuid, summaryId);
		if (data?.ok === false) throw new Error(data.error || "压缩摘要读取失败");
		if (String(baseCompaction.value.summaryId || "") !== summaryId) return;
		loadedCompaction.value = data;
		loadedCompactionSummaryId.value = summaryId;
	} catch (error) {
		compactionError.value = apiError(error);
	} finally {
		compactionLoading.value = false;
	}
}

watch(() => [operationId.value, operationRevision.value], () => {
	const detail = object(loadedToolDetail.value);
	if (
		String(detail.opId || "") !== operationId.value
		|| Number(detail.revision || 0) < operationRevision.value
	) loadedToolDetail.value = null;
	toolDetailError.value = "";
});

watch(() => [props.open, operationId.value, operationRevision.value, detailAvailable.value, isContextCompaction.value], () => {
	void loadToolDetail();
}, {immediate: true});

watch(() => baseCompaction.value.summaryId, (summaryId, previousSummaryId) => {
	if (String(summaryId || "") === String(previousSummaryId || "")) return;
	loadedCompaction.value = null;
	loadedCompactionSummaryId.value = "";
	compactionError.value = "";
});

watch(() => [props.open, baseCompaction.value.summaryId, baseCompaction.value.summaryRef, embeddedCompactionOutput.value], () => {
	void loadCompactionOutput();
}, {immediate: true});

function onToggle(event) {
	emit("toggle", event);
}

function selectCallTab(index) {
	emit("select-tab", index);
}

</script>

<template>
	<details class="tool-event" :class="[`tool-${toolState.status}`]" :open="open" @toggle="onToggle">
		<summary>
			<span class="tool-icon"><component :is="toolState.icon"/></span>
			<span class="tool-name">{{ toolState.title }}</span>
			<span class="disclosure-icon"><ArrowRight/></span>
			<span class="tool-preview">{{ toolState.preview }}</span>
		</summary>
		<div v-if="open" class="tool-detail">
			<div v-if="toolState.isBatch" class="tool-call-selector" role="tablist" aria-label="选择工具调用">
				<button
					v-for="(label, idx) in toolState.tabLabels"
					:key="idx"
					type="button"
					role="tab"
					:aria-selected="activeIndex === idx"
					:class="{active: activeIndex === idx}"
					@click="selectCallTab(idx)"
				>
					{{ label }}
				</button>
			</div>

			<p v-if="toolDetailNotice" class="tool-detail-notice" :class="{'is-error': toolDetailNoticeIsError}">{{ toolDetailNotice }}</p>

			<section class="tool-payload-section" aria-label="调用参数">
				<header class="tool-payload-heading">
					<span class="tool-payload-title">调用参数</span>
					<span v-if="argumentsPayload.content" class="tool-payload-meta">{{ payloadMeta(argumentsPayload) }}</span>
					<button
						v-if="argumentsPayload.content"
						type="button"
						class="tool-payload-copy"
						@click.stop="copyPayload(argumentsPayload, 'arguments')"
					>{{ copiedPayload === 'arguments' ? '已复制' : '复制' }}</button>
				</header>
				<div v-if="argumentsPayload.content" class="tool-payload-code">
					<pre><code class="hljs" :class="argumentsPayload.language ? `language-${argumentsPayload.language}` : ''" v-html="argumentsHtml"></code></pre>
				</div>
				<p v-else class="tool-payload-empty">无调用参数</p>
			</section>

			<section class="tool-payload-section" aria-label="返回结果">
				<header class="tool-payload-heading">
					<span class="tool-payload-title">返回结果</span>
					<span v-if="resultPayload.content" class="tool-payload-meta">{{ payloadMeta(resultPayload) }}</span>
					<button
						v-if="resultPayload.content"
						type="button"
						class="tool-payload-copy"
						@click.stop="copyPayload(resultPayload, 'result')"
					>{{ copiedPayload === 'result' ? '已复制' : '复制' }}</button>
				</header>
				<div v-if="resultPayload.content" class="tool-payload-code">
					<pre><code class="hljs" :class="resultPayload.language ? `language-${resultPayload.language}` : ''" v-html="resultHtml"></code></pre>
				</div>
				<p v-else class="tool-payload-empty" :class="{'is-error': resultNoticeIsError}" aria-live="polite">{{ resultNotice }}</p>
			</section>
		</div>
	</details>
</template>

<style scoped>
.tool-event {
	margin: 0.42rem 0;
	border: 1px solid rgba(15, 23, 42, .10);
	border-radius: 9px;
	background: #fff;
	color: #374151;
	font-size: 12px;
	overflow: hidden;
	box-shadow: none;
}

.tool-event summary {
	display: flex;
	align-items: center;
	gap: 0.46rem;
	cursor: pointer;
	user-select: none;
	list-style: none;
	padding: 0.38rem 0.55rem;
}

.tool-event summary::-webkit-details-marker {
	display: none;
}

.disclosure-icon {
	display: none;
	width: 1rem;
	height: 1rem;
	flex: 0 0 auto;
	place-items: center;
	color: #64748b;
}

summary:hover > .disclosure-icon,
summary:focus-visible > .disclosure-icon,
details[open] > summary > .disclosure-icon {
	display: grid;
}

.disclosure-icon svg {
	width: .72rem;
	height: .72rem;
	transition: transform .14s ease;
}

details[open] > summary > .disclosure-icon svg {
	transform: rotate(90deg);
}

details[open] > summary > .disclosure-icon {
	color: #4338ca;
}

.tool-icon {
	display: grid;
	width: 1.25rem;
	flex: 0 0 auto;
	place-items: center;
	color: #64748b;
}

.tool-icon svg {
	width: 0.95rem;
	height: 0.95rem;
}

.tool-name {
	font-weight: 600;
	color: #111827;
	white-space: nowrap;
}

.tool-preview {
	min-width: 0;
	flex: 1 1 auto;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	color: #6b7280;
}

.tool-detail {
	border-top: 1px solid #e5e7eb;
	background: transparent;
	padding: 0.5rem 0.58rem;
}

.compaction-facts { display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 7px; }
.compaction-facts span { border: 1px solid #e4e7ec; border-radius: 999px; background: #fafafa; padding: 2px 7px; color: #667085; font-size: 10px; }
.compaction-facts .compaction-summary-id { max-width: 100%; overflow: hidden; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; text-overflow: ellipsis; white-space: nowrap; }
.compaction-load-state { margin-bottom: 6px; color: #667085; font-size: 10.5px; }
.compaction-load-state.is-error { color: #b42318; }
.compaction-preview > strong { display: block; margin-bottom: 5px; color: #b26a00; font-size: 10.5px; }

.tool-section + .tool-section {
	margin-top: 0.55rem;
}

.tool-section-title {
	display: inline-flex;
	align-items: center;
	gap: 0.28rem;
	margin-bottom: 0.35rem;
	color: #64748b;
	font-size: 10.5px;
	font-weight: 700;
	letter-spacing: .04em;
	text-transform: uppercase;
}

.tool-section-title svg {
	width: 0.82rem;
	height: 0.82rem;
}

.tool-arg-line pre {
	max-height: 160px;
	overflow: auto;
	margin: 0 0 0.42rem;
	border: 0;
	border-left: 2px solid #e5e7eb;
	border-radius: 0;
	background: transparent;
	padding: 0.18rem 0 0.18rem 0.55rem;
	white-space: pre-wrap;
	color: #334155;
}

.tool-result-tabbar {
	display: flex;
	flex-wrap: wrap;
	gap: .28rem;
	margin-bottom: .42rem;
}

.tool-result-tabbar button {
	border: 1px solid #e2e8f0;
	border-radius: 999px;
	background: #fff;
	padding: .16rem .5rem;
	color: #64748b;
	font-size: 10.5px;
	font-weight: 700;
	cursor: pointer;
}

.tool-result-tabbar button.active {
	border-color: #bfdbfe;
	background: #eff6ff;
	color: #1d4ed8;
}

.tool-result {
	max-height: 360px;
	overflow: auto;
	border: 0;
	border-left: 2px solid #e5e7eb;
	border-radius: 0;
	background: transparent;
	padding: 0.1rem 0 0.1rem 0.6rem;
	box-shadow: none;
}

.tool-result-empty {
	border: 1px dashed #cbd5e1;
	border-radius: 8px;
	background: transparent;
	padding: 0.55rem;
	color: #94a3b8;
}

/* Conversation process annotations: intentionally quiet, inline, and non-card-like. */
.tool-event {
	border: 0;
	border-radius: 0;
	background: transparent;
	box-shadow: none;
	overflow: visible;
	margin: .16rem 0;
	color: #71717a;
	font-size: 11.5px;
}

.tool-event summary {
	display: flex;
	align-items: center;
	gap: .34rem;
	max-width: 100%;
	min-width: 0;
	min-height: 1.45rem;
	padding: .04rem 0;
	color: #71717a;
}

.tool-icon {
	width: .92rem;
	height: .92rem;
	color: #a1a1aa;
}

.tool-icon svg {
	width: .78rem;
	height: .78rem;
	color: currentColor;
}

.tool-name {
	display: block;
	min-width: auto;
	max-width: none;
	flex: 0 0 auto;
	overflow: visible;
	color: #64748b;
	font-weight: 520;
	letter-spacing: 0;
	text-overflow: clip;
	white-space: nowrap;
}

.tool-preview {
	color: #9ca3af;
}

.tool-running > summary .tool-name {
	color: #52525b;
}

.tool-running > summary .tool-preview {
	color: #9ca3af;
}

.tool-running > summary .tool-icon {
	animation: toolLivePulse 1.85s ease-in-out infinite;
	transform-origin: center;
	will-change: opacity, transform;
}

.tool-detail {
	display: grid;
	min-width: 0;
	gap: .72rem;
	margin: .22rem 0 .34rem;
	border-left: 1px solid #e7e9ee;
	padding: .12rem 0 .12rem .72rem;
}

.tool-call-selector {
	display: flex;
	min-width: 0;
	gap: .28rem;
	overflow-x: auto;
	border-bottom: 1px solid #f0f1f4;
	padding: 0 0 .36rem;
}

.tool-call-selector button {
	flex: 0 0 auto;
	border: 0;
	border-bottom: 1px solid transparent;
	background: transparent;
	padding: .08rem .04rem .16rem;
	color: #a1a1aa;
	font-size: 10.5px;
	font-weight: 560;
	cursor: pointer;
}

.tool-call-selector button:hover,
.tool-call-selector button:focus-visible,
.tool-call-selector button.active {
	color: #3f3f46;
}

.tool-call-selector button.active {
	border-bottom-color: #6366f1;
}

.tool-call-selector button:focus-visible,
.tool-payload-copy:focus-visible {
	outline: 2px solid rgba(67,56,202,.18);
	outline-offset: 2px;
}

.tool-detail-notice,
.tool-payload-empty {
	min-width: 0;
	margin: 0;
	color: #a1a1aa;
	font-size: 10.5px;
	line-height: 1.5;
}

.tool-detail-notice.is-error,
.tool-payload-empty.is-error {
	color: #b42318;
}

.tool-payload-section {
	display: grid;
	min-width: 0;
	gap: .32rem;
}

.tool-payload-heading {
	display: flex;
	min-width: 0;
	align-items: baseline;
	gap: .38rem;
}

.tool-payload-title {
	flex: 0 0 auto;
	color: #71717a;
	font-size: 10px;
	font-weight: 620;
	letter-spacing: .035em;
}

.tool-payload-meta {
	min-width: 0;
	margin-left: auto;
	overflow: hidden;
	color: #a1a1aa;
	font-size: 9.5px;
	text-overflow: ellipsis;
	white-space: nowrap;
}

.tool-payload-copy {
	margin-left: auto;
	border: 0;
	background: transparent;
	padding: 0;
	color: #a1a1aa;
	font-size: 9.5px;
	cursor: pointer;
}

.tool-payload-meta + .tool-payload-copy {
	margin-left: 0;
}

.tool-payload-copy:hover {
	color: #3f3f46;
}

.tool-payload-code {
	max-height: min(280px, 34vh);
	overflow: auto;
	border: 1px solid #eceef2;
	border-radius: 7px;
	background: #fcfcfd;
	scrollbar-color: #c7c7cc transparent;
	scrollbar-width: thin;
}

.tool-payload-code pre {
	margin: 0;
	padding: .56rem .64rem;
	white-space: pre;
}

.tool-payload-code code.hljs {
	display: block;
	min-width: max-content;
	background: transparent;
	padding: 0;
	color: #52525b;
	font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
	font-size: 10.5px;
	line-height: 1.55;
	tab-size: 4;
}

@keyframes toolLivePulse {
	0%, 100% {
		opacity: .58;
		transform: scale(.92);
	}
	45% {
		opacity: .96;
		transform: scale(1.04);
	}
}
</style>
