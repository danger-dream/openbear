<script setup>
import {computed, onMounted, onBeforeUnmount, ref, watch} from 'vue';
import {contextMeter} from './conversationWork.js';
import {fmtTokens} from './display.js';
import './conversationWork.css';
const props = defineProps({usage: {type: Object, default: () => ({known: false})}, contextWindow: {type: Number, default: 0}, threshold: {type: Number, default: 0}, strategy: {type: String, default: 'sliding_window'}, conversationUuid: {type: String, default: ''}});
const open = ref(false);
const trigger = ref(null);
function outside(event) {
	if (!trigger.value?.contains(event.target) && !event.target?.closest?.('.context-usage-popover')) open.value = false;
}
onMounted(() => document.addEventListener('pointerdown', outside));
onBeforeUnmount(() => document.removeEventListener('pointerdown', outside));
watch(() => props.conversationUuid, () => { open.value = false; });
const meter = computed(() => contextMeter({...props.usage, window: props.contextWindow, threshold: props.threshold}));
const usedLabel = computed(() => props.usage.known ? fmtTokens(meter.value.used) : '待实测');
const capacityLabel = computed(() => meter.value.capacity ? fmtTokens(meter.value.capacity) : '未声明');
const triggerLabel = computed(() => meter.value.trigger ? fmtTokens(meter.value.trigger) : '未设置');
const triggerPercentLabel = computed(() => meter.value.triggerPercent === null ? '—' : `${meter.value.triggerPercent.toFixed(1)}%`);
const triggerFill = computed(() => meter.value.triggerPercent === null ? 0 : Math.min(100, meter.value.triggerPercent));
const triggerSummary = computed(() => `上下文：已用 ${usedLabel.value} / 压缩阈值 ${triggerLabel.value}，${triggerPercentLabel.value}`);
function touchOpen(event) {
	if (event.pointerType === 'touch') open.value = !open.value;
}
</script>

<template>
	<el-popover v-model:visible="open" trigger="hover" placement="top-end" :width="'min(304px, calc(100vw - 24px))'" :show-arrow="false" :show-after="100" :hide-after="120" :offset="8" transition="context-usage-fade" popper-class="context-usage-popover">
		<template #reference>
			<button ref="trigger" type="button" class="context-usage-trigger" :class="[`is-${meter.tone}`, {'is-unknown': meter.triggerPercent === null}]" :aria-label="triggerSummary" :aria-expanded="open" @pointerdown="touchOpen" @focus="open = true" @blur="open = false" @keydown.esc.stop="open = false" @keydown.enter.prevent="open = !open" @keydown.space.prevent="open = !open">
				<svg viewBox="0 0 24 24" aria-hidden="true"><circle class="context-ring-track" cx="12" cy="12" r="9"/><circle class="context-ring-fill" cx="12" cy="12" r="9" pathLength="100" :stroke-dasharray="`${triggerFill} 100`"/></svg>
				<span class="context-inline-value">{{ usedLabel }} / {{ triggerLabel }}</span><span class="context-inline-percent">{{ triggerPercentLabel }}</span>
				<span class="context-inline-strategy">· {{ props.strategy === 'model_summary' ? '摘要压缩' : '滑窗压缩' }}</span>
			</button>
		</template>
		<section class="context-usage-content" :aria-label="triggerSummary" @keydown.esc.stop="open = false">
			<header><strong>上下文用量</strong><span>{{ usedLabel }} / {{ triggerLabel }} <small>（{{ triggerPercentLabel }}）</small></span></header>
			<div class="context-capacity-track" role="progressbar" aria-label="压缩阈值占用" :aria-valuenow="meter.triggerPercent === null ? undefined : triggerFill" :aria-valuetext="triggerSummary" aria-valuemin="0" aria-valuemax="100"><span :style="{width: `${triggerFill}%`}"/></div>
			<dl>
				<div><dt>已用上下文</dt><dd>{{ props.usage.known ? `${meter.used.toLocaleString()} tokens` : '待下次调用实测' }}</dd></div>
				<div><dt>模型窗口</dt><dd>{{ capacityLabel }}</dd></div>
				<div><dt>{{ props.strategy === 'model_summary' ? '摘要压缩阈值' : '滑动窗口阈值' }}</dt><dd>{{ triggerLabel }}</dd></div>
				<div v-if="meter.triggerPercent !== null"><dt>阈值占用</dt><dd :class="`is-${meter.tone}`">{{ meter.triggerPercent.toFixed(1) }}%</dd></div>
			</dl>
			<p>{{ props.usage.known ? '最近一次模型调用的上下文用量，非累计 Tokens。' : '当前用量尚无有效实测值，不以 0% 代替。' }}</p>
		</section>
	</el-popover>
</template>

<style scoped>
.context-usage-trigger { display: inline-flex; flex: none; align-items: center; justify-content: center; width: auto; height: 28px; gap: 5px; padding: 0 4px; font-size: 10px; font-variant-numeric: tabular-nums; white-space: nowrap; border: 0; border-radius: 7px; background: transparent; color: var(--work-muted); cursor: pointer; transition: background 120ms ease, color 120ms ease; }
.context-usage-trigger:hover, .context-usage-trigger[aria-expanded="true"] { background: var(--work-hover); color: var(--work-text); }
.context-usage-trigger:focus-visible { outline: 2px solid var(--bear-accent, var(--ob-focus)); outline-offset: 2px; }
.context-usage-trigger svg { width: 12px; height: 12px; flex: none; transform: rotate(-90deg); }
.context-inline-value { color: var(--ob-chat-subtle); }
.context-inline-percent { color: var(--ob-chat-muted); }
.context-inline-strategy { display: none; }
.context-ring-track, .context-ring-fill { fill: none; stroke: currentColor; stroke-width: 3; }
.context-ring-track { opacity: .22; }
.context-ring-fill { stroke-linecap: round; opacity: .85; transition: stroke-dasharray 250ms ease; }
.is-unknown .context-ring-track { stroke-dasharray: 3 3; }
.context-usage-trigger.is-warning, .context-usage-content .is-warning { color: var(--ob-warning); }
.context-usage-trigger.is-danger, .context-usage-content .is-danger { color: var(--ob-danger); }
.context-usage-content { padding: 14px; color: var(--work-text); font-size: 12px; line-height: 1.6; }
.context-usage-content header { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 4px 10px; }
.context-usage-content strong { font-size: 13px; font-weight: 550; }
.context-usage-content header > span { color: var(--work-muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.context-usage-content small { font-size: inherit; }
.context-capacity-track { height: 5px; overflow: hidden; margin: 12px 0 14px; border-radius: 4px; background: var(--work-hover); }
.context-capacity-track > span { display: block; height: 100%; border-radius: inherit; background: var(--ob-chat-subtle); transition: width 250ms ease; }
.context-usage-content dl { display: grid; gap: 7px; margin: 0; }
.context-usage-content dl > div { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; }
.context-usage-content dt { color: var(--work-muted); }
.context-usage-content dd { margin: 0; font-variant-numeric: tabular-nums; }
.context-usage-content p { margin: 12px 0 0; border-top: 1px solid var(--work-rule); padding-top: 10px; color: var(--work-faint); font-size: 11px; }
@media (max-width: 760px) {
	.context-usage-trigger { width: max-content; max-width: 100%; height: 32px; font-size: 9px; gap: 6px; }
	.context-inline-strategy { display: inline; color: var(--ob-chat-muted); }
}
@media (prefers-reduced-motion: reduce) { .context-usage-trigger, .context-ring-fill, .context-capacity-track > span { transition: none; } }
</style>
<style>
.el-popover.el-popper.context-usage-popover { padding: 0; border: 1px solid var(--work-rule); border-radius: 12px; background: var(--work-panel); box-shadow: var(--ob-shadow-popover); }
.context-usage-fade-enter-active, .context-usage-fade-leave-active { transition: opacity 120ms ease, transform 120ms ease; transform-origin: bottom right; }
.context-usage-fade-enter-from, .context-usage-fade-leave-to { opacity: 0; transform: translateY(3px) scale(.97); }
@media (prefers-reduced-motion: reduce) { .context-usage-fade-enter-active, .context-usage-fade-leave-active { transition: none; } }
</style>
