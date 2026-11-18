<script setup>
import {computed, onBeforeUnmount, onMounted, ref, watch} from "vue";
import {IMAGE_ONE_TURN_ROWS, imageOneTurnLabel, normalizeImageOneTurn} from "./imageOneTurn.js";

// The composer's 图片只看一轮 switches. A desktop popover; on a phone a bottom sheet whose
// header follows a downward drag and slides out, as the extensions one does. The composer
// renders the trigger through the slot and owns the setting: this only reports changes.
// The two follow-up switches keep their values while the main one is off.
const props = defineProps({
	value: {type: Object, default: () => ({})},
	saving: {type: Boolean, default: false},
	draft: {type: Boolean, default: false},
	conversationUuid: {type: String, default: ""},
});
const emit = defineEmits(["change"]);

const open = ref(false), phone = ref(false);
const setting = computed(() => normalizeImageOneTurn(props.value));
const label = computed(() => imageOneTurnLabel(setting.value));
const summary = computed(() => setting.value.enabled ? "已开启" : "关闭");
// The phone composer lists this menu in its tools sheet and opens it from there.
defineExpose({show: () => { open.value = true; }, summary});
const note = computed(() => props.draft ? "发送第一条消息时生效" : `本会话 · ${setting.value.enabled ? "已开启" : "关闭"}`);
const rows = IMAGE_ONE_TURN_ROWS;
const disabled = row => props.saving || (row.sub && !setting.value.enabled);
function set(row, on) {
	if (!disabled(row)) emit("change", {[row.key]: Boolean(on)});
}

let media = null;
function updateMedia() { phone.value = Boolean(media?.matches); }
onMounted(() => { media = window.matchMedia?.("(max-width: 760px)") || null; updateMedia(); media?.addEventListener("change", updateMedia); });
onBeforeUnmount(() => { media?.removeEventListener("change", updateMedia); });
watch(() => props.conversationUuid, (now, before) => {
	// The draft became a real conversation: its panel stays as it is.
	if (!String(before || "").startsWith("local:")) open.value = false;
});

const sheetOffset = ref(0), sheetDragging = ref(false), sheetClosing = ref(false);
let gesture = null;
const sheetStyle = computed(() => sheetOffset.value
	? {"--iot-drag-y": `${sheetOffset.value}px`, transform: sheetClosing.value ? undefined : `translateY(${sheetOffset.value}px)`}
	: undefined);
function dragStart(event) {
	if (sheetClosing.value || event.button > 0 || event.target.closest("button, input, a, .el-switch")) return;
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
	<el-popover v-if="!phone" v-model:visible="open" trigger="click" placement="top-start" :show-arrow="false" :hide-after="0"
	            popper-class="composer-menu-popper image-one-turn-popper" :width="'min(23rem, calc(100vw - 2rem))'">
		<template #reference>
			<span class="iot-trigger"><slot name="trigger" :label="label" :open="open" :on="setting.enabled"/></span>
		</template>
		<div class="iot-panel" role="dialog" aria-label="图片只看一轮">
			<div class="iot-head"><span class="iot-title">图片只看一轮</span><span class="iot-note">{{ note }}</span></div>
			<ul class="iot-list">
				<li v-for="row in rows" :key="row.key" class="iot-row" :class="{'is-sub': row.sub, 'is-first-sub': row.key === 'includeUser', 'is-disabled': row.sub && !setting.enabled}">
					<div class="iot-text"><span class="iot-label">{{ row.label }}</span><span class="iot-hint" :title="row.hint">{{ row.hint }}</span></div>
					<el-switch :model-value="setting[row.key]" size="small" :disabled="disabled(row)" :aria-label="row.label" @update:model-value="set(row, $event)"/>
				</li>
			</ul>
			<p class="iot-foot">模型需要时会按路径重新读取原图。开启后的下一轮，会把之前的图片一次性换掉；关闭后已换掉的不会恢复。</p>
		</div>
	</el-popover>
	<template v-else>
		<span class="iot-trigger" @click="open = true"><slot name="trigger" :label="label" :open="open" :on="setting.enabled"/></span>
		<el-drawer v-model="open" direction="btt" size="auto" append-to-body :with-header="false"
		           :class="['image-one-turn-sheet', 'mobile-viewport-sheet', {'is-dragging': sheetDragging}]" :style="sheetStyle"
		           modal-class="image-one-turn-overlay" aria-label="图片只看一轮" @closed="sheetClosed">
			<div class="iot-panel">
				<header class="iot-head iot-sheet-head" @pointerdown="dragStart" @pointermove="dragMove" @pointerup="dragEnd" @pointercancel="dragCancel">
					<div class="iot-grip" aria-hidden="true"></div>
					<span class="iot-title">图片只看一轮</span><span class="iot-note">{{ note }}</span>
					<button type="button" class="iot-close" aria-label="关闭图片只看一轮" @click="open = false">×</button>
				</header>
				<ul class="iot-list">
					<li v-for="row in rows" :key="row.key" class="iot-row" :class="{'is-sub': row.sub, 'is-first-sub': row.key === 'includeUser', 'is-disabled': row.sub && !setting.enabled}">
						<div class="iot-text"><span class="iot-label">{{ row.label }}</span><span class="iot-hint">{{ row.hint }}</span></div>
						<el-switch :model-value="setting[row.key]" :disabled="disabled(row)" :aria-label="row.label" @update:model-value="set(row, $event)"/>
					</li>
				</ul>
				<p class="iot-foot">模型需要时会按路径重新读取原图。开启后的下一轮，会把之前的图片一次性换掉；关闭后已换掉的不会恢复。</p>
			</div>
		</el-drawer>
	</template>
</template>

<style>
.iot-trigger { display: inline-flex; }
.image-one-turn-popper.el-popper { padding: 0; }
.iot-panel { display: flex; flex-direction: column; min-width: 0; }
.iot-head { display: flex; align-items: baseline; gap: .5rem; padding: .6rem .75rem .35rem; }
.iot-title { font-size: 13px; font-weight: 600; color: var(--ob-text-strong); }
.iot-note { min-width: 0; font-size: 11.5px; color: var(--ob-text-muted); }
.iot-list { margin: 0; padding: 0 .25rem; list-style: none; }
.iot-row { display: flex; align-items: center; gap: .6rem; min-height: 40px; padding: .35rem .5rem; border-radius: 8px; }
.iot-row:hover { background: var(--ob-hover); }
.iot-row.is-disabled:hover { background: transparent; }
.iot-row.is-first-sub { position: relative; margin-top: .5rem; }
.iot-row.is-first-sub::before { content: ""; position: absolute; top: -.25rem; left: 0; right: 0; border-top: 1px solid var(--ob-border-soft); }
.iot-text { display: flex; flex: 1; flex-direction: column; min-width: 0; }
.iot-label { font-size: 13px; color: var(--ob-text); }
.iot-hint { overflow: hidden; font-size: 11.5px; line-height: 1.45; color: var(--ob-text-muted); text-overflow: ellipsis; white-space: nowrap; }
.iot-row.is-disabled .iot-label, .iot-row.is-disabled .iot-hint { color: var(--ob-text-disabled); }
.iot-foot { margin: .25rem .75rem .6rem; font-size: 11px; line-height: 1.55; color: var(--ob-text-muted); }
.iot-close { display: none; }
@media (max-width: 760px) {
	.image-one-turn-sheet.el-drawer.btt { border-radius: 16px 16px 0 0; background: var(--ob-surface-raised); transition: transform 280ms cubic-bezier(.22, .68, 0, 1); }
	html .image-one-turn-sheet.mobile-viewport-sheet > .el-drawer__body { padding: 0 0 max(10px, env(safe-area-inset-bottom, 0px)); }
	.image-one-turn-sheet.is-dragging { transition: none; user-select: none; }
	.image-one-turn-overlay.el-drawer-fade-leave-active { transition: background-color 280ms linear; }
	.image-one-turn-overlay.el-drawer-fade-leave-from .image-one-turn-sheet { transform: translateY(var(--iot-drag-y, 0px)); }
	.image-one-turn-overlay.el-drawer-fade-leave-active .image-one-turn-sheet { pointer-events: none; }
	.iot-sheet-head { position: relative; align-items: center; padding: 20px max(16px, env(safe-area-inset-right, 0px)) 6px max(16px, env(safe-area-inset-left, 0px)); touch-action: none; }
	.iot-grip { position: absolute; top: 8px; left: calc(50% - 15px); width: 30px; height: 4px; border-radius: 2px; background: var(--ob-border-strong); }
	.iot-title { font-size: 15px; }
	.iot-note { flex: 1; font-size: 12px; }
	.iot-close { display: grid; place-items: center; width: 36px; height: 36px; margin: -6px -8px -6px 0; border: 0; border-radius: 999px; background: transparent; color: var(--ob-text-subtle); font-size: 20px; }
	.iot-list { padding: 0 max(8px, env(safe-area-inset-right, 0px)) 0 max(8px, env(safe-area-inset-left, 0px)); }
	.iot-row { min-height: 52px; padding: .4rem .6rem; }
	.iot-label { font-size: 14px; }
	.iot-hint { font-size: 12px; white-space: normal; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
	.iot-foot { margin: .35rem max(16px, env(safe-area-inset-right, 0px)) .4rem max(16px, env(safe-area-inset-left, 0px)); font-size: 12px; }
}
</style>
