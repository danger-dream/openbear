<script setup>
import {computed, inject, ref, watch} from 'vue';
import {ChevronRight} from '@lucide/vue';
import WorkDisclosure from './WorkDisclosure.vue';
import {conversationWorkChunks, lastAnswerIndex, workDurationLabel} from './conversationWork.js';
import {collabStatusLabel, collabToolCall, useCollabRequests} from './collabRequests.js';
import {AGENT_LOCATOR, agentEventIdentity} from './agentPanelState.js';
import './conversationWork.css';

const props = defineProps({
	turn: {type: Object, required: true}, entries: {type: Array, default: () => []},
	running: Boolean, durationMs: {type: Number, default: 0},
});
const resultIndex = computed(() => lastAnswerIndex(props.turn.events));
const chunks = computed(() => conversationWorkChunks(props.entries, resultIndex.value));
const entryIndices = computed(() => new Map(props.entries.map((entry, index) => [entry, index])));
const firstWork = computed(() => chunks.value.find(chunk => chunk.kind === 'work')?.key);
const open = ref(props.running || resultIndex.value < 0);
watch(() => props.running, running => {
	open.value = running || resultIndex.value < 0;
}, {immediate: true});
const agentLocator = inject(AGENT_LOCATOR, null);
watch(() => agentLocator?.request.value.seq, () => {
	const key = agentLocator?.request.value.key;
	if (key && chunks.value.some(chunk => chunk.kind === 'work' && chunk.entries.some(entry => agentEventIdentity(entry.event) === key))) open.value = true;
});
const label = computed(() => `已工作${workDurationLabel(props.durationMs) ? ` ${workDurationLabel(props.durationMs)}` : ''}`);
// A folded block must not hide how requests to other conversations ended.
const collabStore = useCollabRequests();
const requestSummary = computed(() => {
	const calls = props.entries.map(entry => collabToolCall(entry.event)).filter(call => call?.action === 'send');
	if (!calls.length) return '';
	const live = calls.length === 1 ? collabStore?.get(calls[0].requestId) || collabStore?.forCall(calls[0].toolCallId) : null;
	return live ? `「${live.to?.title || '另一个会话'}」${collabStatusLabel(live)}` : `${calls.length} 个会话请求`;
});
function entryKey(entry) { return `${entry.event.id || entry.event.eventKey || entry.event.message?.id || entry.event.operation?.opId || entry.index}:${entry.part || 'event'}`; }
</script>

<template>
	<div class="conversation-work" :class="{'is-working': props.running}">
		<template v-for="chunk in chunks" :key="chunk.key">
			<template v-if="chunk.kind === 'work'">
				<button v-if="!props.running && chunk.key === firstWork" type="button" class="work-heading" :aria-expanded="open" @click="open = !open">
					<span>{{ label }}</span>
					<span v-if="requestSummary && !open" class="work-heading-note">· {{ requestSummary }}</span>
					<ChevronRight class="work-chevron" :stroke-width="1.75" :class="{'is-open': open}"/>
				</button>
				<WorkDisclosure :open="props.running || open">
					<div class="work-stack">
						<template v-for="entry in chunk.entries" :key="entryKey(entry)"><slot :entry="entry" :conversation-index="entryIndices.get(entry)" :work="true"/></template>
					</div>
				</WorkDisclosure>
			</template>
			<div v-else class="work-exposed-stack">
				<template v-for="entry in chunk.entries" :key="entryKey(entry)"><slot :entry="entry" :conversation-index="entryIndices.get(entry)" :work="false"/></template>
			</div>
		</template>
	</div>
</template>

<style scoped>
.conversation-work { min-width: 0; }
.work-heading { display: flex; align-items: center; gap: 8px; width: 100%; padding: 6px 0 10px; margin: 0; border: 0; border-bottom: 1px solid var(--work-summary-rule); background: transparent; color: var(--work-muted); font: inherit; font-size: 13px; line-height: 1.6; text-align: left; cursor: pointer; }
.work-heading:focus-visible { outline: 2px solid var(--bear-accent, var(--ob-focus)); outline-offset: 3px; border-radius: 4px; }
.work-heading:hover { color: var(--work-text); }
.work-heading-note { min-width: 0; overflow: hidden; color: var(--work-faint); text-overflow: ellipsis; white-space: nowrap; }
.work-chevron { width: 13px; height: 13px; flex: none; transition: transform 200ms ease, opacity 200ms ease; opacity: .65; }
.work-chevron.is-open { transform: rotate(90deg); }
/* Both intra-group and inter-group spacing come from the same leading space.
   No trailing margin survives a collapsed work chunk between retry rows. */
.work-stack, .work-exposed-stack { display: flex; flex-direction: column; gap: 16px; min-width: 0; padding: 16px 0 0; }
.work-exposed-stack:first-child, .work-disclosure:first-child .work-stack { padding-top: 0; }
@media (prefers-reduced-motion: reduce) { .work-chevron { transition: none; } }
</style>
