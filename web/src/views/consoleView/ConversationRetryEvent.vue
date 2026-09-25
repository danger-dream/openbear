<script setup>
import {computed} from 'vue';
import {RotateCw, CircleCheck, CircleX, CircleMinus, Clock3} from '@lucide/vue';
import {modelRetryReasonLabel} from './display.js';
import {retryWaitLabel, retryStatusView} from './modelRetryPresentation.js';

const props = defineProps({event: {type: Object, required: true}, retryActionPending: {type: Object, default: () => ({})}});
const emit = defineEmits(['cancel-retry', 'retry-now']);
const retry = computed(() => props.event.retry || {});
const status = computed(() => retryStatusView(retry.value));
const attempt = computed(() => {
	const current = Number(retry.value.attempt || 0), maximum = Number(retry.value.maxAttempts || 0);
	return current && maximum ? `${current}/${maximum}` : current ? String(current) : '—';
});
const description = computed(() => [modelRetryReasonLabel(retry.value), retryWaitLabel(retry.value)].filter(Boolean).join(' · ') || status.value.label);
const outcomeIcon = computed(() => ({success: CircleCheck, failed: CircleX, cancelled: CircleMinus, waiting: Clock3})[status.value.tone] || RotateCw);
const pending = computed(() => props.retryActionPending?.waitId === retry.value.waitId);
</script>

<template>
	<div class="conversation-retry retry-inline-event" role="status" aria-live="polite">
		<div class="retry-summary">
			<RotateCw class="retry-icon" :stroke-width="1.75" aria-hidden="true"/>
			<span class="retry-kind" :class="{'work-status-sweep': retry.active}">模型重试</span>
			<span class="retry-attempt">{{ attempt }}</span>
			<span class="retry-description" :title="description">{{ description }}</span>
			<span class="retry-outcome" :class="`is-${status.tone}`">
				<component :is="outcomeIcon" :stroke-width="1.75" aria-hidden="true"/>
				<span>{{ status.label }}</span>
			</span>
			<span v-if="retry.active && retry.cancellable && retry.waitId" class="retry-actions">
				<button type="button" class="retry-action retry-action-now" :disabled="pending" @click.stop="emit('retry-now', props.event)">{{ pending && props.retryActionPending.action === 'retry' ? '请求中…' : '立即重试' }}</button>
				<button type="button" class="retry-action retry-action-cancel" :disabled="pending" @click.stop="emit('cancel-retry', props.event)">{{ pending && props.retryActionPending.action === 'cancel' ? '取消中…' : '取消重试' }}</button>
			</span>
		</div>
	</div>
</template>

<style scoped>
.conversation-retry { min-width: 0; margin: 0; color: var(--work-muted); font-size: 14px; line-height: 1.65; }
.retry-summary { display: grid; grid-template-columns: 16px auto auto minmax(0, 1fr) auto; align-items: center; gap: 8px; min-width: 0; min-height: 24px; }
.retry-summary:has(.retry-actions) { grid-template-columns: 16px auto auto minmax(0, 1fr) auto auto; }
.retry-icon { width: 16px; height: 16px; color: var(--work-faint); }
.retry-kind { color: var(--work-faint); font-weight: 400; white-space: nowrap; }
.retry-attempt { color: var(--work-faint); font-size: 13px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.retry-description { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.retry-outcome { display: inline-flex; align-items: center; gap: 4px; color: var(--work-faint); font-size: 12px; white-space: nowrap; }
.retry-outcome svg { width: 12px; height: 12px; flex: none; }
.retry-outcome.is-failed { color: var(--ob-danger); }
.retry-actions { display: inline-flex; align-items: center; gap: 8px; }
.retry-action { border: 0; border-radius: 4px; background: transparent; padding: 2px 0; color: var(--work-muted); font: inherit; font-size: 12px; cursor: pointer; white-space: nowrap; }
.retry-action:hover:not(:disabled) { color: var(--work-text); text-decoration: underline; text-underline-offset: 3px; }
.retry-action:focus-visible { outline: 2px solid var(--bear-accent, var(--ob-focus)); outline-offset: 3px; }
.retry-action:disabled { opacity: .55; cursor: wait; }
@media (max-width: 760px) {
	.retry-summary { min-height: 32px; }
	.retry-summary:has(.retry-actions) { grid-template-columns: 16px auto auto minmax(0, 1fr) auto; }
	.retry-actions { grid-column: 2 / -1; }
	.retry-action { min-height: 32px; }
}
</style>
