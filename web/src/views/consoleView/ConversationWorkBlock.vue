<script setup>
import {computed, ref, watch} from 'vue';
import {ChevronRight} from '@lucide/vue';
import WorkDisclosure from './WorkDisclosure.vue';
import {conversationWorkChunks, lastAnswerIndex, workDurationLabel} from './conversationWork.js';
import './conversationWork.css';

const props = defineProps({
	turn: {type: Object, required: true}, entries: {type: Array, default: () => []},
	running: Boolean, durationMs: {type: Number, default: 0},
});
const resultIndex = computed(() => lastAnswerIndex(props.turn.events));
const chunks = computed(() => conversationWorkChunks(props.entries, resultIndex.value));
const firstWork = computed(() => chunks.value.find(chunk => chunk.kind === 'work')?.key);
const open = ref(props.running || resultIndex.value < 0);
watch(() => props.running, running => {
	open.value = running || resultIndex.value < 0;
}, {immediate: true});
const label = computed(() => `已工作${workDurationLabel(props.durationMs) ? ` ${workDurationLabel(props.durationMs)}` : ''}`);
function entryKey(entry) { return `${entry.event.id || entry.event.eventKey || entry.event.message?.id || entry.event.operation?.opId || entry.index}:${entry.part || 'event'}`; }
</script>

<template>
	<div class="conversation-work" :class="{'is-working': props.running}">
		<template v-for="chunk in chunks" :key="chunk.key">
			<template v-if="chunk.kind === 'work'">
				<button v-if="!props.running && chunk.key === firstWork" type="button" class="work-heading" :aria-expanded="open" @click="open = !open">
					<span>{{ label }}</span>
					<ChevronRight class="work-chevron" :stroke-width="1.75" :class="{'is-open': open}"/>
				</button>
				<WorkDisclosure :open="props.running || open">
					<div class="work-stack">
						<template v-for="entry in chunk.entries" :key="entryKey(entry)"><slot :entry="entry" :conversation-index="props.entries.indexOf(entry)" :work="true"/></template>
					</div>
				</WorkDisclosure>
			</template>
			<div v-else class="work-exposed-stack">
				<template v-for="entry in chunk.entries" :key="entryKey(entry)"><slot :entry="entry" :conversation-index="props.entries.indexOf(entry)" :work="false"/></template>
			</div>
		</template>
	</div>
</template>

<style scoped>
.conversation-work { min-width: 0; }
.work-heading { display: flex; align-items: center; gap: 8px; width: 100%; padding: 6px 0 10px; margin: 0; border: 0; border-bottom: 1px solid var(--work-summary-rule); background: transparent; color: var(--work-muted); font: inherit; font-size: 13px; line-height: 1.6; text-align: left; cursor: pointer; }
.work-heading:focus-visible { outline: 2px solid var(--bear-accent, var(--ob-focus)); outline-offset: 3px; border-radius: 4px; }
.work-heading:hover { color: var(--work-text); }
.work-chevron { width: 13px; height: 13px; flex: none; transition: transform 200ms ease, opacity 200ms ease; opacity: .65; }
.work-chevron.is-open { transform: rotate(90deg); }
/* Both intra-group and inter-group spacing come from the same leading space.
   No trailing margin survives a collapsed work chunk between retry rows. */
.work-stack, .work-exposed-stack { display: flex; flex-direction: column; gap: 16px; min-width: 0; padding: 16px 0 0; }
.work-exposed-stack:first-child, .work-disclosure:first-child .work-stack { padding-top: 0; }
@media (prefers-reduced-motion: reduce) { .work-chevron { transition: none; } }
</style>
