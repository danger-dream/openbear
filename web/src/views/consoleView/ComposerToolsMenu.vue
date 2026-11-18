<script setup>
import {computed, ref, watch} from "vue";

// The phone composer's single tools entry. Its bottom sheet lists the actions the
// toolbar shows one by one on wider screens; choosing a row closes the sheet and
// reports the row's key. Rows that open their own sheet are opened by the composer.
const props = defineProps({
	items: {type: Array, default: () => []},
	conversationUuid: {type: String, default: ""},
});
const emit = defineEmits(["select"]);

const open = ref(false);
function choose(item) {
	if (item.disabled) return;
	open.value = false;
	emit("select", item.key);
}
watch(() => props.conversationUuid, (now, before) => {
	if (!String(before || "").startsWith("local:")) open.value = false;
});

const sheetOffset = ref(0), sheetDragging = ref(false), sheetClosing = ref(false);
let gesture = null;
const sheetStyle = computed(() => sheetOffset.value
	? {"--tools-drag-y": `${sheetOffset.value}px`, transform: sheetClosing.value ? undefined : `translateY(${sheetOffset.value}px)`}
	: undefined);
function dragStart(event) {
	if (sheetClosing.value || event.button > 0 || event.target.closest("button, input, a")) return;
	gesture = {id: event.pointerId, x: event.clientX, y: event.clientY, lastY: event.clientY, lastT: event.timeStamp, speed: 0};
	event.currentTarget.setPointerCapture?.(event.pointerId);
}
function dragMove(event) {
	if (!gesture || event.pointerId !== gesture.id) return;
	const dx = event.clientX - gesture.x, dy = event.clientY - gesture.y;
	if (!sheetDragging.value) {
		if (Math.hypot(dx, dy) < 4) return;
		if (dy < 0 || Math.abs(dx) > Math.abs(dy)) { dragCancel(); return; }
		sheetDragging.value = true;
	}
	gesture.speed = (event.clientY - gesture.lastY) / Math.max(1, event.timeStamp - gesture.lastT);
	gesture.lastY = event.clientY; gesture.lastT = event.timeStamp;
	sheetOffset.value = Math.max(0, dy);
}
function dragCancel() { gesture = null; sheetDragging.value = false; sheetOffset.value = 0; }
function dragEnd() {
	if (!gesture) return;
	if (sheetOffset.value >= 88 || (sheetOffset.value > 24 && gesture.speed > 0.5)) {
		gesture = null; sheetDragging.value = false; sheetClosing.value = true; open.value = false;
	} else dragCancel();
}
function sheetClosed() { sheetClosing.value = false; sheetOffset.value = 0; }
</script>

<template>
	<span class="tools-trigger" @click="open = true"><slot name="trigger" :open="open"/></span>
	<el-drawer v-model="open" direction="btt" size="auto" append-to-body :with-header="false"
	           :class="['tools-sheet', 'mobile-viewport-sheet', {'is-dragging': sheetDragging}]" :style="sheetStyle"
	           modal-class="tools-overlay" aria-label="功能" @closed="sheetClosed">
		<div class="tools-panel">
			<header class="tools-head" @pointerdown="dragStart" @pointermove="dragMove" @pointerup="dragEnd" @pointercancel="dragCancel">
				<div class="tools-grip" aria-hidden="true"></div>
				<span class="tools-title">功能</span>
				<button type="button" class="tools-close" aria-label="关闭功能" @click="open = false">×</button>
			</header>
			<ul class="tools-list" role="menu" aria-label="功能">
				<li v-for="item in items" :key="item.key" role="menuitem" class="tools-row"
				    :class="{'is-disabled': item.disabled, 'is-on': item.on}" :aria-disabled="item.disabled || undefined" :aria-label="item.attention ? item.label + '：模板已删除，重组上下文时使用系统默认' : undefined"
				    :tabindex="item.disabled ? -1 : 0" @click="choose(item)" @keydown.enter.prevent="choose(item)" @keydown.space.prevent="choose(item)">
					<span class="tools-icon" aria-hidden="true"><component :is="item.icon" :stroke-width="1.6"/><span v-if="item.attention" class="tools-attention">!</span></span>
					<span class="tools-label">{{ item.label }}</span>
					<span v-if="item.hint" class="tools-hint">{{ item.hint }}</span>
					<svg v-if="item.more" class="tools-more" viewBox="0 0 24 24" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>
				</li>
			</ul>
		</div>
	</el-drawer>
</template>

<style>
.tools-trigger { display: inline-flex; }
.tools-sheet.el-drawer.btt { border-radius: 16px 16px 0 0; background: var(--ob-surface-raised); transition: transform 280ms cubic-bezier(.22, .68, 0, 1); }
html .tools-sheet.mobile-viewport-sheet > .el-drawer__body { padding: 0 0 max(10px, env(safe-area-inset-bottom, 0px)); }
.tools-sheet.is-dragging { transition: none; user-select: none; }
.tools-overlay.el-drawer-fade-leave-active { transition: background-color 280ms linear; }
.tools-overlay.el-drawer-fade-leave-from .tools-sheet { transform: translateY(var(--tools-drag-y, 0px)); }
.tools-overlay.el-drawer-fade-leave-active .tools-sheet { pointer-events: none; }
.tools-panel { display: flex; flex-direction: column; min-width: 0; }
.tools-head { position: relative; display: flex; align-items: center; gap: .5rem; padding: 20px max(16px, env(safe-area-inset-right, 0px)) 6px max(16px, env(safe-area-inset-left, 0px)); touch-action: none; }
.tools-grip { position: absolute; top: 8px; left: calc(50% - 15px); width: 30px; height: 4px; border-radius: 2px; background: var(--ob-border-strong); }
.tools-title { flex: 1; font-size: 15px; font-weight: 600; color: var(--ob-text-strong); }
.tools-close { display: grid; place-items: center; width: 36px; height: 36px; margin: -6px -8px -6px 0; border: 0; border-radius: 999px; background: transparent; color: var(--ob-text-subtle); font-size: 20px; }
.tools-list { margin: 0; padding: 0 max(8px, env(safe-area-inset-right, 0px)) 4px max(8px, env(safe-area-inset-left, 0px)); list-style: none; max-height: calc(var(--mobile-viewport-height, 100dvh) * .7); overflow-y: auto; }
.tools-row { display: flex; align-items: center; gap: .7rem; min-height: 52px; padding: .35rem .6rem; border-radius: 10px; cursor: pointer; outline: none; }
.tools-row:active, .tools-row:focus-visible { background: var(--ob-hover); }
.tools-row.is-disabled { cursor: default; opacity: .45; }
.tools-row.is-disabled:active { background: transparent; }
.tools-icon { position: relative; display: grid; flex: none; place-items: center; width: 34px; height: 34px; border-radius: 9px; background: var(--ob-hover); color: var(--ob-text); }
.tools-attention { position: absolute; top: 1px; right: 1px; display: grid; place-items: center; width: 11px; height: 11px; border-radius: 50%; background: var(--ob-warning); color: var(--ob-surface); font-size: 9px; font-weight: 700; line-height: 1; }
.tools-icon svg { width: 18px; height: 18px; }
.tools-row.is-on .tools-icon { background: var(--ob-blue-soft); color: var(--ob-blue); }
.tools-label { flex: none; font-size: 14px; color: var(--ob-text); }
.tools-hint { flex: 1; min-width: 0; overflow: hidden; font-size: 12.5px; color: var(--ob-text-muted); text-align: right; text-overflow: ellipsis; white-space: nowrap; }
.tools-row.is-on .tools-hint { color: var(--ob-blue); }
.tools-more { flex: none; width: 16px; height: 16px; margin-left: auto; fill: none; stroke: var(--ob-text-muted); stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
.tools-hint + .tools-more { margin-left: 0; }
</style>
