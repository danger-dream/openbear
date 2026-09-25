<script setup>
import {computed, ref} from 'vue';
import {Brain, SquareTerminal, Search, FileText, FilePenLine, PencilLine, Globe, History, BookOpen, Wrench, ChevronRight, ListChecks} from '@lucide/vue';
import ConsoleToolEvent from './ConsoleToolEvent.vue';
import WorkDisclosure from './WorkDisclosure.vue';
import {eventPrimaryToolName, toolDisplayState, toolPreview as argumentToolPreview, toolStatus} from './display.js';
import {latestReasoningLine, processToolLabel, reasoningDuration, workDurationLabel} from './conversationWork.js';

const props = defineProps({event: {type: Object, required: true}, part: {type: String, default: ''},
	conversationUuid: {type: String, default: ''}, activeIndex: {type: Number, default: 0}});
const emit = defineEmits(['select-tab']);
const open = ref(false);
const groupActive = ref({});
const isReasoning = computed(() => props.part === 'reasoning' || props.event.kind === 'answer');
const reasoning = computed(() => String(props.event.message?.reasoning || ''));
const groupEvents = computed(() => Array.isArray(props.event.events) ? props.event.events : []);
const state = computed(() => {
	const value = toolDisplayState(props.event, props.activeIndex);
	if (!groupEvents.value.length) return value;
	const statuses = groupEvents.value.map(toolStatus);
	return {...value, status: statuses.includes('running') ? 'running' : statuses.includes('error') ? 'error' : 'ok'};
});
const running = computed(() => isReasoning.value ? Boolean(props.event.reasoningActive) : state.value.status === 'running');
const line = computed(() => latestReasoningLine(reasoning.value));
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
const processIcon = computed(() => isReasoning.value ? Brain : groupEvents.value.length ? ListChecks : ({
	Bash: SquareTerminal, Read: FileText, Write: FilePenLine, Edit: PencilLine, EditBatch: PencilLine,
	Browser: Globe, History, Memory: BookOpen, TaskMemory: BookOpen,
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
const vSummaryTail = {
	mounted(el) {
		let observedText;
		const sync = () => {
			// Track text width too, as ZCode does, not only the fixed viewport.
			const text = el.firstElementChild;
			if (observedText !== text) {
				if (observedText) observer?.unobserve(observedText);
				observedText = text;
				if (text) observer?.observe(text);
			}
			el.scrollLeft = el.scrollWidth;
			el.dataset.overflow = String(el.scrollWidth > el.clientWidth + 1);
		};
		const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(sync) : null;
		observer?.observe(el);
		el._workSync = sync;
		el._workCleanup = () => { observer?.disconnect(); el._workLineAnimation?.cancel(); };
		sync();
	},
	updated(el, {value, oldValue}) {
		// Keep the same visible node and sweep animation. New tokens never queue;
		// only a new line gets a short motion, without fading through blank space.
		if (value?.key !== oldValue?.key) {
			el._workLineAnimation?.cancel();
			if (!window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
				el._workLineAnimation = el.firstElementChild?.animate?.(
					[{transform: 'translateY(.25em)'}, {transform: 'translateY(0)'}],
					{duration: 120, easing: 'cubic-bezier(.2,.7,.2,1)'},
				);
			}
		}
		el._workSync?.();
	},
	beforeUnmount(el) { el._workCleanup?.(); },
};
</script>

<template>
	<div class="conversation-process" :class="{'is-running': running, 'is-open': open}">
		<button type="button" class="process-summary" :aria-expanded="open" @click="open = !open">
			<component :is="processIcon" class="process-icon" :stroke-width="1.75" aria-hidden="true"/>
			<span class="process-copy">
			<span class="process-kind" :class="{'work-status-sweep': running}">{{ isReasoning ? (running ? '正在思考' : '思考') : toolLabel }}</span>
			<template v-if="isReasoning">
				<span v-if="running && !open && line.text" class="process-separator">·</span>
				<span v-if="running && !open && line.text" v-summary-tail="line" class="reasoning-preview">
					<span class="reasoning-preview-line work-status-sweep">{{ line.text }}</span>
				</span>
				<span v-else-if="!running && duration" class="process-meta">· 持续了 {{ duration }}</span>
			</template>
			<template v-else>
				<span v-if="state.status === 'error'" class="process-error">未成功</span>
				<span v-if="toolPreview" class="process-preview" :class="{'work-status-sweep': running}" :title="toolPreview">{{ toolPreview }}</span>
			</template>
			</span>
			<ChevronRight class="process-chevron" :stroke-width="1.75" aria-hidden="true"/>
		</button>
		<WorkDisclosure :open="open">
			<div v-if="isReasoning" class="process-reasoning-detail">
				<div v-reasoning-scroll class="process-reasoning-scroll" tabindex="0" aria-label="思考内容，可滚动"><div>{{ reasoning }}</div></div>
			</div>
			<div v-else class="process-tool-detail">
				<template v-if="groupEvents.length"><ConsoleToolEvent v-for="(event, i) in groupEvents" :key="event.id || i" :event="event" :conversation-uuid="props.conversationUuid" :open="true" :active-index="groupActive[event.id || i] || 0" @select-tab="groupActive[event.id || i] = $event"/></template>
				<ConsoleToolEvent v-else :event="props.event" :conversation-uuid="props.conversationUuid" :open="true" detail-only :active-index="props.activeIndex" @select-tab="emit('select-tab', $event)"/>
			</div>
		</WorkDisclosure>
	</div>
</template>

<style scoped>
.conversation-process { min-width: 0; color: var(--work-muted); font-size: 14px; line-height: 1.65; }
.process-summary { display: flex; align-items: center; gap: 8px; max-width: 100%; min-width: 0; min-height: 24px; margin: 0; padding: 0; border: 0; background: none; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.process-summary:focus-visible { outline: 2px solid var(--bear-accent, var(--ob-focus)); outline-offset: 4px; border-radius: 3px; }
.process-icon { width: 16px; height: 16px; flex: none; align-self: center; color: var(--work-faint); }
.process-copy { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.process-kind { flex: none; color: var(--work-faint); font-weight: 400; white-space: nowrap; }
.process-meta, .process-separator { flex: none; color: var(--work-faint); font-size: 13px; }
.process-preview { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--work-muted); }
.process-error { flex: none; color: var(--ob-danger); font-size: 12px; white-space: nowrap; }
.process-chevron { width: 13px; height: 13px; flex: none; align-self: center; opacity: 0; color: var(--work-faint); transition: opacity 160ms ease, transform 200ms ease; }
.process-summary:hover .process-chevron, .process-summary:focus-visible .process-chevron, .is-open > .process-summary .process-chevron { opacity: 1; }
.is-open > .process-summary .process-chevron { transform: rotate(90deg); }
.reasoning-preview { display: block; min-width: 0; overflow: hidden; white-space: nowrap; height: 1.65em; color: var(--work-muted); }
.reasoning-preview[data-overflow="true"] { mask-image: linear-gradient(to right, transparent, #000 14px, #000 calc(100% - 14px), transparent); }
.reasoning-preview-line { display: inline-block; white-space: nowrap; }
.process-reasoning-detail { padding-top: 12px; }
.process-reasoning-scroll { max-height: min(260px, 42vh); overflow: auto; margin-left: 7px; border-left: 1px solid var(--work-rule); padding: 2px 14px; color: var(--work-muted); white-space: pre-wrap; overflow-wrap: anywhere; scrollbar-width: thin; overscroll-behavior: contain; }
.process-reasoning-scroll[data-mask-bottom="true"] { mask-image: linear-gradient(to bottom, #000 calc(100% - 16px), transparent); }
.process-reasoning-scroll[data-mask-top="true"] { mask-image: linear-gradient(to bottom, transparent, #000 16px); }
.process-reasoning-scroll[data-mask-top="true"][data-mask-bottom="true"] { mask-image: linear-gradient(to bottom, transparent, #000 16px, #000 calc(100% - 16px), transparent); }
.process-tool-detail { min-width: 0; padding-top: 10px; }
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
@media (hover: none) { .process-chevron { opacity: .6; } .process-summary { min-height: 32px; } }
@media (prefers-reduced-motion: reduce) {
	.process-chevron { transition: none; }
}
</style>
