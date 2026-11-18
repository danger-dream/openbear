<script setup>
import {computed, onBeforeUnmount, ref, watch} from "vue";
import {ElDialog, ElDrawer, ElMessage} from "element-plus";
import {Api, apiError} from "../api.js";

const props = defineProps({modelValue: Boolean});
const emit = defineEmits(["update:modelValue", "synced"]);
const items = ref([]), selected = ref([]), loading = ref(false), saving = ref(false), error = ref("");
let alive = true, request = 0;
const media = window.matchMedia("(max-width: 760px)");
const phone = ref(media.matches);
const updateMedia = () => { phone.value = media.matches; };
media.addEventListener("change", updateMedia);
const sheetOffset = ref(0), sheetDragging = ref(false), sheetClosing = ref(false);
let gesture = null;
const sheetStyle = computed(() => phone.value && sheetOffset.value
	? {"--local-personas-drag-y": `${sheetOffset.value}px`, transform: sheetClosing.value ? undefined : `translateY(${sheetOffset.value}px)`}
	: undefined);
function dragStart(event) {
	if (!phone.value || saving.value || sheetClosing.value || event.button > 0 || event.target.closest("button, input, a")) return;
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
	if (!saving.value && (sheetOffset.value >= 88 || (sheetOffset.value > 24 && gesture.speed > 0.5))) {
		gesture = null; sheetDragging.value = false; sheetClosing.value = true; open.value = false;
	} else dragCancel();
}
function sheetClosed() { sheetClosing.value = false; dragCancel(); }
const pending = item => item.status === "available" || item.status === "update";
const statusText = item => ({available: "尚未同步", update: "本地资源有新版，将新增副本", existing: "已同步", modified: "已同步 · 保留你的修改"})[item.status] || "";
const open = computed({get: () => props.modelValue, set: value => { if (!saving.value) emit("update:modelValue", value); }});
async function load() {
	const seq = ++request;
	loading.value = true; error.value = ""; items.value = []; selected.value = [];
	try {
		const result = await Api.localPersonas();
		if (!alive || seq !== request) return;
		items.value = result.items || [];
		selected.value = items.value.filter(pending).map(item => item.id);
	} catch (e) { if (alive && seq === request) error.value = `读取失败：${apiError(e)}`; }
	finally { if (alive && seq === request) loading.value = false; }
}
async function sync() {
	if (saving.value || loading.value || !selected.value.length) return;
	saving.value = true; error.value = "";
	try {
		const result = await Api.syncLocalPersonas([...selected.value]);
		if (result?.ok === false) throw new Error(result.error || "同步失败");
		if (!alive) return;
		ElMessage.success(result.created ? `已同步 ${result.created} 个人设，可在新会话中选择` : "所选人设已同步，未重复创建");
		emit("synced");
		emit("update:modelValue", false);
	} catch (e) { if (alive) error.value = `同步失败：${apiError(e)}`; }
	finally { saving.value = false; }
}
watch(() => props.modelValue, value => { if (value) void load(); else request++; }, {immediate: true});
onBeforeUnmount(() => { alive = false; request++; media.removeEventListener("change", updateMedia); });
</script>

<template>
	<component :is="phone ? ElDrawer : ElDialog" v-model="open" append-to-body class="settings-ui local-personas-dialog"
	           :class="phone ? ['local-personas-sheet', 'mobile-viewport-sheet', {'is-dragging': sheetDragging}] : 'admin-dialog'"
	           title="同步本地人设" width="520px" direction="btt" size="auto" :style="sheetStyle" modal-class="local-personas-overlay"
	           :close-on-click-modal="!saving" :close-on-press-escape="!saving" :show-close="!saving" @closed="sheetClosed">
		<template #header>
			<div class="local-personas-heading" @pointerdown="dragStart" @pointermove="dragMove" @pointerup="dragEnd" @pointercancel="dragCancel">
				<span v-if="phone" class="local-personas-grip" aria-hidden="true"></span>
				<span>同步本地人设</span>
			</div>
		</template>
		<p class="local-personas-hint">从当前安装包读取人设，保存为可选择、编辑的模板副本。不会自动启用，也不改变已有会话。</p>
		<div v-if="loading" class="local-personas-empty" role="status">正在读取本地资源…</div>
		<el-checkbox-group v-else-if="items.length" v-model="selected" class="local-personas-list" aria-label="本地人设">
			<el-checkbox v-for="item in items" :key="item.id" :value="item.id" :disabled="saving || !pending(item)" class="local-personas-item">
				<span class="local-personas-name">{{ item.name }}</span>
				<span class="local-personas-status">{{ statusText(item) }}</span>
			</el-checkbox>
		</el-checkbox-group>
		<div v-else-if="!error" class="local-personas-empty">当前安装包没有附带人设。</div>
		<p v-if="error" class="local-personas-error" role="alert">{{ error }} <button v-if="!items.length" type="button" @click="load">重新读取</button></p>
		<p class="local-personas-footnote">重复同步不会新增。你编辑过的内容会保留；资源更新时另存副本，不覆盖原模板。</p>
		<template #footer>
			<el-button :disabled="saving" @click="open = false">关闭</el-button>
			<el-button type="primary" :loading="saving" :disabled="loading || !selected.length" @click="sync">同步所选人设</el-button>
		</template>
	</component>
</template>

<style>
.local-personas-heading { color: var(--ob-text-strong); font-size: 15px; font-weight: 600; }
.local-personas-hint, .local-personas-footnote { margin: 0; color: var(--ob-text-subtle); font-size: 12px; line-height: 1.65; overflow-wrap: anywhere; }
.local-personas-footnote { padding-top: 12px; border-top: 1px solid var(--ob-border); }
.local-personas-list { display: flex; flex-direction: column; margin: 12px 0; }
.local-personas-item.el-checkbox { height: auto; min-height: 48px; margin: 0; padding: 7px 0; }
.local-personas-item .el-checkbox__label { min-width: 0; white-space: normal; overflow-wrap: anywhere; }
.local-personas-name, .local-personas-status { display: block; }
.local-personas-name { color: var(--ob-text); font-size: 13px; line-height: 1.5; }
.local-personas-status { color: var(--ob-text-subtle); font-size: 12px; line-height: 1.5; font-weight: 400; }
.local-personas-empty { padding: 20px 0; color: var(--ob-text-subtle); font-size: 13px; }
.local-personas-error { color: var(--ob-danger); font-size: 12px; overflow-wrap: anywhere; }
.local-personas-error button { background: none; border: 0; color: var(--ob-blue); cursor: pointer; font: inherit; padding: 8px; }
@media (max-width: 760px) {
	.local-personas-sheet.el-drawer.btt { max-height: calc(var(--mobile-viewport-height, 100dvh) * .88); border-radius: 16px 16px 0 0; background: var(--ob-surface-raised); transition: transform 280ms cubic-bezier(.22, .68, 0, 1); }
	.local-personas-sheet > .el-drawer__header { margin: 0; padding: 0 max(12px, env(safe-area-inset-right, 0px)) 0 max(16px, env(safe-area-inset-left, 0px)); border-bottom: 1px solid var(--ob-border); }
	.local-personas-sheet .local-personas-heading { display: flex; flex: 1; align-items: center; height: 56px; padding-top: 8px; touch-action: none; }
	.local-personas-sheet .el-drawer__close-btn { width: 40px; height: 40px; margin-top: 8px; }
	.local-personas-grip { position: absolute; top: 8px; left: calc(50% - 15px); width: 30px; height: 4px; border-radius: 2px; background: var(--ob-border-strong); }
	html .local-personas-sheet.mobile-viewport-sheet > .el-drawer__body { flex: 0 1 auto; overflow-y: auto; padding: 12px max(16px, env(safe-area-inset-right, 0px)) 14px max(16px, env(safe-area-inset-left, 0px)); }
	.local-personas-sheet > .el-drawer__footer { padding: 10px max(16px, env(safe-area-inset-right, 0px)) max(12px, env(safe-area-inset-bottom, 0px)) max(16px, env(safe-area-inset-left, 0px)); border-top: 1px solid var(--ob-border); }
	.local-personas-sheet .local-personas-name { font-size: 14px; }
	.local-personas-sheet.el-drawer.btt.is-dragging { transition: none; user-select: none; }
	.local-personas-overlay.el-drawer-fade-leave-active { transition: background-color 280ms linear; }
	.local-personas-overlay.el-drawer-fade-leave-from .local-personas-sheet { transform: translateY(var(--local-personas-drag-y, 0px)); }
	.local-personas-overlay.el-drawer-fade-leave-active .local-personas-sheet { pointer-events: none; }
}
</style>
