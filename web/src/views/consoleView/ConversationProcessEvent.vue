<script setup>
import {computed, inject, ref, watch} from 'vue';
import {Brain, SquareTerminal, Search, FileText, FilePenLine, PencilLine, Globe, History, BookOpen, Wrench, ChevronRight, ChevronUp, ListChecks, MessagesSquare, FilePlus, MessageSquareReply, Eye} from '@lucide/vue';
import ConsoleToolEvent from './ConsoleToolEvent.vue';
import WorkDisclosure from './WorkDisclosure.vue';
import {eventPrimaryToolName, toolDisplayState, toolPreview as argumentToolPreview, toolStatus} from './display.js';
import {processToolLabel, reasoningDuration, workDurationLabel, WORK_MOTION} from './conversationWork.js';
import {reasoningPreviewState, reasoningPreviewDirective as vReasoningPreview} from './reasoningPreview.js';
import CollabRequestDetail from './CollabRequestDetail.vue';
import {openArtifactPreview} from '../../artifacts/artifactFiles.js';
import {toolFileIdentityFor} from './toolFileAction.js';
import {collabStateTone, collabStatusLabel, collabToolCall, OPEN_STATES, useCollabRequests} from './collabRequests.js';
import {useDeferredReasoning} from './reasoningDetail.js';
import {useWorkItems, workItemCallNumber} from './workItems.js';

const props = defineProps({event: {type: Object, required: true}, part: {type: String, default: ''},
	conversationUuid: {type: String, default: ''}, activeIndex: {type: Number, default: 0}});
const emit = defineEmits(['select-tab']);
const open = ref(false);
const groupActive = ref({});
const summaryButton = ref(null);
const toolContent = ref(null);
const showCollapse = ref(false);
const motion = inject(WORK_MOTION, null);
watch(toolContent, (el, _, onCleanup) => {
	showCollapse.value = false;
	if (!el) return;
	const viewport = el.closest('.console-scroll');
	const measure = () => {
		const height = viewport?.clientHeight || window.innerHeight || 600;
		showCollapse.value = el.getBoundingClientRect().height > Math.min(320, Math.max(140, height * .6));
	};
	measure();
	if (typeof ResizeObserver === 'undefined') return;
	const observer = new ResizeObserver(measure);
	observer.observe(el);
	if (viewport) observer.observe(viewport);
	onCleanup(() => observer.disconnect());
}, {flush: 'post'});
function collapseTool() {
	motion?.reveal?.(summaryButton.value);
	summaryButton.value?.focus({preventScroll: true});
	open.value = false;
}
const isReasoning = computed(() => props.part === 'reasoning' || props.event.kind === 'answer');
const deferredReasoning = useDeferredReasoning({
	event: () => props.event,
	open: () => open.value && isReasoning.value,
	conversationUuid: () => props.conversationUuid,
});
const reasoning = computed(() => deferredReasoning.text.value);
const groupEvents = computed(() => Array.isArray(props.event.events) ? props.event.events : []);
const state = computed(() => {
	const value = toolDisplayState(props.event, props.activeIndex);
	if (!groupEvents.value.length) return value;
	const statuses = groupEvents.value.map(toolStatus);
	return {...value, status: statuses.includes('running') ? 'running' : statuses.includes('error') ? 'error' : 'ok'};
});
const running = computed(() => isReasoning.value ? Boolean(props.event.reasoningActive) : state.value.status === 'running');
const preview = computed(() => reasoningPreviewState(reasoning.value));
const duration = computed(() => workDurationLabel(reasoningDuration(props.event)));
const toolName = computed(() => {
	const primary = eventPrimaryToolName(props.event);
	return primary === 'Tool' ? props.event.name || primary : primary;
});
const toolLabel = computed(() => processToolLabel(toolName.value, state.value.title));
// Main-chat summaries describe the task, not the changing stdout/PID preview.
// Keep that progress payload intact for the expanded tool/work details.
const toolPreview = computed(() => props.event.calls?.length
	? argumentToolPreview({...props.event, live: false})
	: props.event.preview || state.value.preview || '');
// A request to another conversation reads as who/what/where-it-stands, with
// the live request state rather than the moment-of-sending tool result.
const collabStore = useCollabRequests();
const collab = computed(() => {
	const call = isReasoning.value ? null : collabToolCall(props.event);
	if (call?.action === 'respond') return respondRow(call);
	if (!call || !['send', 'supply'].includes(call.action)) return null;
	const live = collabStore?.get(call.requestId) || collabStore?.forCall(call.toolCallId) || null;
	const failed = call.result?.ok === false || Boolean(call.result?.error);
	const supply = call.action === 'supply';
	return {
		requestId: call.requestId || live?.requestId || '', live, failed, supply, expandable: true,
		label: supply ? '补充材料' : '会话请求',
		target: live?.to?.title || '',
		title: live?.title || call.args.title || '',
		// Material belongs to its request; the request row carries the outcome.
		status: failed ? '未发出' : supply ? '已补充' : collabStatusLabel(live) || call.result?.stateLabel || '',
		tone: failed ? 'danger' : supply ? 'info' : live ? collabStateTone(live.state) : 'info',
		waiting: !failed && !supply && (!live || OPEN_STATES.has(live.state)),
		failure: call.result?.message || call.result?.error || '',
	};
});
const fileIdentity = computed(() => isReasoning.value || collab.value ? null : toolFileIdentityFor(props.event, props.conversationUuid));
function openFile(event) {
	if (fileIdentity.value) openArtifactPreview(fileIdentity.value, '', event.currentTarget);
}
const workItems = useWorkItems();
// A WorkItem row opens the task panel at the item it changed.
const workItemNumber = computed(() => {
	if (isReasoning.value || !workItems || toolName.value !== 'WorkItem') return '';
	const call = props.event.calls?.[0];
	return call ? workItemCallNumber(call.arguments || call.previewArguments || '', props.event.result?.content || '') : '';
});
function openWorkItem() {
	if (workItemNumber.value) workItems.open(workItemNumber.value);
}
// The receiver's own declaration: what it told the requester, in its words.
function respondRow(call) {
	const outcome = {completed: '完成', needs_input: '需要补充', rejected: '拒绝'}[call.args.outcome] || '完成';
	const failed = call.result?.ok === false || Boolean(call.result?.error);
	return {
		respond: true, requestId: '', label: '答复请求', target: '', title: String(call.args.text || '').trim(), expandable: String(call.args.text || '').trim().length > 60,
		status: failed ? '未记录' : outcome, failed, failure: call.result?.message || '',
		tone: failed ? 'danger' : call.args.outcome === 'rejected' ? 'danger'
			: call.args.outcome === 'needs_input' ? 'warning' : 'success',
		waiting: false,
	};
}
const processIcon = computed(() => isReasoning.value ? Brain : collab.value?.respond ? MessageSquareReply : collab.value ? (collab.value.supply ? FilePlus : MessagesSquare) : groupEvents.value.length ? ListChecks : ({
	Bash: SquareTerminal, Read: FileText, Write: FilePenLine, Edit: PencilLine, EditBatch: PencilLine,
	Browser: Globe, History, Memory: BookOpen, TaskMemory: BookOpen, WorkItem: ListChecks,
	mcp__parrot__web_search: Search, mcp__parrot__web_fetch: Globe,
})[toolName.value] || Wrench);

// Follow only the user's current reading intent. Neither new chunks nor a
// resize may pull a reader back down after they scroll up inside the reasoning.
const vReasoningScroll = {
	mounted(el) {
		let follow = true;
		const sync = () => { if (follow) el.scrollTop = el.scrollHeight; updateMask(); };
		const updateMask = () => {
			el.dataset.maskTop = String(el.scrollTop > 2);
			el.dataset.maskBottom = String(el.scrollHeight - el.clientHeight - el.scrollTop > 2);
		};
		const scroll = () => { follow = el.scrollHeight - el.clientHeight - el.scrollTop <= 2; updateMask(); };
		el.addEventListener?.('scroll', scroll, {passive: true});
		const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(sync) : null;
		observer?.observe(el);
		if (el.firstElementChild) observer?.observe(el.firstElementChild);
		el._workSync = sync;
		el._workCleanup = () => { observer?.disconnect(); el.removeEventListener?.('scroll', scroll); };
		sync();
	},
	updated(el) { el._workSync?.(); },
	beforeUnmount(el) { el._workCleanup?.(); },
};
</script>

<template>
	<div class="conversation-process" :class="{'is-running': running, 'is-open': open, 'is-reasoning': isReasoning}">
		<div class="process-row">
			<button ref="summaryButton" type="button" class="process-summary" :aria-expanded="open" :disabled="collab && !collab.expandable && !collab.failed" @click="open = !open">
				<component :is="processIcon" class="process-icon" :stroke-width="1.75" aria-hidden="true"/>
				<span class="process-copy">
				<span class="process-kind" :class="{'work-status-sweep': running}">{{ isReasoning ? (running ? '正在思考' : '思考') : collab ? collab.label : toolLabel }}</span>
				<template v-if="isReasoning">
					<span v-if="running && !open && preview.text" class="process-separator">·</span>
					<span v-if="running && !open && preview.text" v-reasoning-preview="preview" class="reasoning-preview">
						<span class="reasoning-preview-line"></span>
					</span>
					<span v-else-if="!running && duration" class="process-meta">· 持续了 {{ duration }}</span>
				</template>
				<template v-else-if="collab">
					<span v-if="collab.target" class="process-request-target">{{ collab.target }}</span>
					<span class="process-preview" :title="collab.title">{{ collab.title }}</span>
					<span v-if="collab.status" class="process-request-status" :class="[`tone-${collab.tone}`, {'work-status-sweep': collab.waiting}]">{{ collab.status }}</span>
				</template>
				<template v-else>
					<span v-if="state.status === 'error'" class="process-error">未成功</span>
					<span v-if="toolPreview" class="process-preview" :class="{'work-status-sweep': running}" :title="toolPreview">{{ toolPreview }}</span>
				</template>
				</span>
				<ChevronRight v-if="!fileIdentity && !workItemNumber && (!collab || collab.expandable || collab.failed)" class="process-chevron" :stroke-width="1.75" aria-hidden="true"/>
			</button>
			<button v-if="workItemNumber" type="button" class="process-file-open" title="打开任务" :aria-label="`打开任务 #${workItemNumber}`" @click="openWorkItem"><Eye :stroke-width="1.75" aria-hidden="true"/></button>
			<ChevronRight v-if="workItemNumber" class="process-chevron" :stroke-width="1.75" aria-hidden="true" @click="open = !open"/>
			<button v-if="fileIdentity" type="button" class="process-file-open" title="打开文件" :aria-label="`打开文件：${toolPreview}`" @click="openFile"><Eye :stroke-width="1.75" aria-hidden="true"/></button>
			<!-- After the eye, so the eye sits right next to the file name. -->
			<ChevronRight v-if="fileIdentity" class="process-chevron" :stroke-width="1.75" aria-hidden="true" @click="open = !open"/>
		</div>
		<WorkDisclosure :open="open">
			<div v-if="isReasoning" class="process-reasoning-detail">
				<div v-reasoning-scroll class="process-reasoning-scroll" tabindex="0" aria-label="思考内容，可滚动"><div>{{ reasoning || (deferredReasoning.error.value ? `思考内容读取失败：${deferredReasoning.error.value}` : deferredReasoning.loading.value ? '正在读取思考内容…' : '') }}</div></div>
			</div>
			<div v-else-if="collab" class="process-tool-detail">
				<div class="process-request-detail">
					<p v-if="collab.failed" class="process-error">{{ collab.failure || (collab.respond ? '结果没有记录' : '请求没有发出') }}</p>
					<p v-else-if="collab.respond" class="process-respond-text">{{ collab.title || '没有附说明' }}</p>
					<CollabRequestDetail v-else-if="collab.requestId" :request-id="collab.requestId" :request="collab.live" perspective="sender" @changed="collabStore?.load()"/>
				</div>
			</div>
			<div v-else class="process-tool-detail">
				<div ref="toolContent" class="process-tool-content">
					<template v-if="groupEvents.length"><ConsoleToolEvent v-for="(event, i) in groupEvents" :key="event.id || i" :event="event" :conversation-uuid="props.conversationUuid" :open="true" :active-index="groupActive[event.id || i] || 0" @select-tab="groupActive[event.id || i] = $event"/></template>
					<ConsoleToolEvent v-else :event="props.event" :conversation-uuid="props.conversationUuid" :open="true" detail-only :active-index="props.activeIndex" @select-tab="emit('select-tab', $event)"/>
				</div>
				<div v-if="showCollapse" class="process-collapse-dock">
					<button type="button" class="process-collapse-button" aria-label="收起工具详情" @click.stop="collapseTool"><ChevronUp :size="16" aria-hidden="true"/><span>收起</span></button>
				</div>
			</div>
		</WorkDisclosure>
	</div>
</template>

<style scoped>
.conversation-process { min-width: 0; color: var(--work-muted); font-size: 14px; line-height: 1.65; }
/* Native viewport rendering keeps DOM, find-in-page, selection and component
   state intact. Only settled, collapsed single-line rows have a known height:
   never guess a placeholder for a growing answer, open/closing detail or active control.
   Browsers without support simply retain the original rendering path. */
.conversation-process:not(.is-open):not(.is-running):not(:has(.work-disclosure)) {
	content-visibility: auto;
	contain-intrinsic-block-size: auto 24px;
	overflow-clip-margin: 8px;
}
.process-row { display: flex; align-items: center; gap: 8px; min-width: 0; }
.process-summary { display: flex; align-items: center; gap: 8px; max-width: 100%; min-width: 0; min-height: 24px; margin: 0; padding: 0; border: 0; background: none; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.process-summary:disabled { cursor: default; color: inherit; }
.process-summary:focus-visible { outline: 2px solid var(--bear-accent, var(--ob-focus)); outline-offset: 4px; border-radius: 3px; }
.process-icon { width: 16px; height: 16px; flex: none; align-self: center; color: var(--work-faint); }
.process-copy { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.process-kind { flex: none; color: var(--work-faint); font-weight: 400; white-space: nowrap; }
.process-meta, .process-separator { flex: none; color: var(--work-faint); font-size: 13px; }
.process-preview { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--work-muted); }
.process-error { flex: none; color: var(--ob-danger); font-size: 12px; white-space: nowrap; }
.process-file-open { position: relative; display: grid; width: 22px; height: 22px; flex: none; place-items: center; margin: 0 0 0 -2px; padding: 0; border: 0; border-radius: 6px; background: none; color: var(--work-faint); cursor: pointer; transition: color 120ms ease, background-color 120ms ease; }
.process-file-open svg { width: 15px; height: 15px; }
/* Touch-sized hit area without making the row taller. */
.process-file-open::after { content: ""; position: absolute; inset: -5px; }
.process-file-open:hover { background: var(--ob-hover); color: var(--work-text, var(--ob-text)); }
.process-file-open:focus-visible { outline: 2px solid var(--bear-accent, var(--ob-focus)); outline-offset: 2px; }
.process-request-target { flex: 0 1 auto; min-width: 2em; max-width: 9em; overflow: hidden; color: var(--work-text, var(--ob-text)); text-overflow: ellipsis; white-space: nowrap; }
.process-request-status { flex: none; font-size: 12px; white-space: nowrap; color: var(--work-faint); }
.process-request-status.tone-success { color: var(--ob-success); }
.process-request-status.tone-warning { color: var(--ob-warning); }
.process-request-status.tone-danger { color: var(--ob-danger); }
.process-respond-text { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.process-request-detail { min-width: 0; margin-left: 7px; border-left: 1px solid var(--work-rule); padding: 2px 0 2px 16px; color: var(--work-detail-text); font-size: 13px; }
.process-chevron { width: 13px; height: 13px; flex: none; align-self: center; opacity: 0; color: var(--work-faint); transition: opacity 160ms ease, transform 200ms ease; }
.process-summary:hover .process-chevron, .process-summary:focus-visible .process-chevron, .is-open > .process-row > .process-summary .process-chevron { opacity: 1; }
.is-open > .process-row > .process-summary .process-chevron { transform: rotate(90deg); }
.process-row > .process-chevron { margin-left: -2px; cursor: pointer; }
.process-row:has(> .process-summary:hover) > .process-chevron, .process-row > .process-chevron:hover, .process-summary:focus-visible ~ .process-chevron, .is-open > .process-row > .process-chevron { opacity: 1; }
.is-open > .process-row > .process-chevron { transform: rotate(90deg); }
.is-reasoning > .process-row > .process-summary { width: 100%; }
.is-reasoning > .process-row > .process-summary > .process-copy { flex: 1; }
.reasoning-preview { display: block; flex: 1; min-width: 0; overflow: hidden; white-space: nowrap; height: 1.65em; color: var(--work-muted); }
.reasoning-preview[data-overflow="true"] { mask-image: linear-gradient(to right, transparent, #000 14px); }
.reasoning-preview-line { display: inline-block; white-space: nowrap; }
.process-reasoning-detail { padding-top: 12px; }
.process-reasoning-scroll { max-height: min(260px, 42vh); overflow: auto; margin-left: 7px; border-left: 1px solid var(--work-rule); padding: 2px 14px; color: var(--work-muted); white-space: pre-wrap; overflow-wrap: anywhere; scrollbar-width: thin; overscroll-behavior: contain; }
.process-reasoning-scroll[data-mask-bottom="true"] { mask-image: linear-gradient(to bottom, #000 calc(100% - 16px), transparent); }
.process-reasoning-scroll[data-mask-top="true"] { mask-image: linear-gradient(to bottom, transparent, #000 16px); }
.process-reasoning-scroll[data-mask-top="true"][data-mask-bottom="true"] { mask-image: linear-gradient(to bottom, transparent, #000 16px, #000 calc(100% - 16px), transparent); }
.process-tool-detail { min-width: 0; padding-top: 10px; }
.process-tool-content { min-width: 0; }
.process-collapse-dock { display: none; }
@media (max-width: 760px) {
	.process-collapse-dock { display: flex; justify-content: flex-end; position: sticky; bottom: 12px; z-index: 3; height: 52px; padding: 8px 8px 0; pointer-events: none; }
	.process-collapse-button { display: inline-flex; align-items: center; justify-content: center; gap: 5px; min-width: 80px; height: 44px; padding: 0 12px; border: 1px solid var(--ob-chat-border); border-radius: 999px; background: rgb(var(--ob-surface-rgb) / .88); color: var(--ob-chat-text); box-shadow: 0 4px 18px rgb(var(--ob-shadow-rgb) / .12); -webkit-backdrop-filter: blur(14px); backdrop-filter: blur(14px); font: inherit; font-size: 12px; cursor: pointer; pointer-events: auto; }
	.process-collapse-button:focus-visible { outline: 2px solid var(--ob-focus); outline-offset: 3px; }
}
/* One quiet shell, like ZCode's terminal panel — not a card around a second
   bordered detail box around a third set of bordered code blocks. */
.process-tool-detail :deep(.tool-event) { margin: 0; border: 1px solid var(--work-panel-rule); border-radius: 12px; background: var(--work-panel); color: var(--work-detail-text); font-size: 13px; box-shadow: none; }
.process-tool-detail :deep(.tool-event + .tool-event) { margin-top: 12px; }
.process-tool-detail :deep(.tool-detail) { margin: 0; border: 0; border-radius: 0; background: transparent; padding: 12px 16px; gap: 16px; }
.process-tool-detail :deep(.tool-payload-heading) { gap: 8px; }
.process-tool-detail :deep(.tool-payload-title) { color: var(--work-muted); font-size: 12px; font-weight: 400; letter-spacing: 0; }
.process-tool-detail :deep(.tool-payload-meta), .process-tool-detail :deep(.tool-payload-copy) { color: var(--work-faint); font-size: 11px; }
.process-tool-detail :deep(.tool-payload-copy:hover) { color: var(--work-detail-text); }
.process-tool-detail :deep(.tool-payload-code) { border: 0; border-radius: 0; background: transparent; }
.process-tool-detail :deep(.tool-payload-code pre) { padding: 2px 0; font-size: 12px; line-height: 1.65; }
.process-tool-detail :deep(.tool-payload-code code.hljs) { color: var(--work-detail-text); font-size: 12px; line-height: 1.65; }
@media (max-width: 640px) {
	.process-kind:has(+ .process-request-target) { display: none; }
}
@media (hover: none) {
	.process-chevron { opacity: .6; }
	.process-summary { min-height: 32px; }
	.conversation-process:not(.is-open):not(.is-running):not(:has(.work-disclosure)) { contain-intrinsic-block-size: auto 32px; }
}
@media (prefers-reduced-motion: reduce) {
	.process-chevron { transition: none; }
}
</style>
