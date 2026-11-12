<script setup>
import {computed, onBeforeUnmount, onMounted, ref, watch} from "vue";
import {Api} from "../../api.js";
import {fmtTokens} from "./display.js";
import {extensionDraft, setExtensionDraft, onExtensionDraftForgotten} from "./extensionDrafts.js";

// The composer's extension switches: optional tool groups of this conversation. A desktop
// popover; on a phone a bottom sheet whose header follows a downward drag and slides out.
// The composer renders the trigger through the slot, so it shares its toolbar buttons' styles.
// A conversation not yet created (a local draft) starts from its folder's default and keeps
// its choices locally until the first send creates it with them.
const props = defineProps({
	conversationUuid: {type: String, default: ""}, folderId: {type: String, default: ""}, running: {type: Boolean, default: false},
});

const state = ref(null), loading = ref(false), saving = ref(""), error = ref("");
const open = ref(false), phone = ref(false);
const draft = computed(() => props.conversationUuid.startsWith("local:"));
const usable = computed(() => Boolean(props.conversationUuid));
const identity = computed(() => JSON.stringify([props.conversationUuid, draft.value ? props.folderId : ""]));
const canChange = computed(() => usable.value && state.value && !saving.value);
const items = computed(() => state.value?.extensions || []);
const enabledCount = computed(() => items.value.filter(item => item.enabled).length);
const label = computed(() => state.value ? `扩展（已开启 ${enabledCount.value} 个）` : "扩展");
const note = computed(() => draft.value ? "发送第一条消息时生效" : "开关从下一次请求起生效");
let generation = 0, media = null;

function resetState() {
	++generation;
	state.value = null; error.value = ""; saving.value = ""; loading.value = false;
}
async function load() {
	// A refresh would discard the answer of the write under way.
	if (saving.value) return;
	const current = ++generation, conversation = props.conversationUuid;
	error.value = "";
	if (!usable.value) { state.value = null; return; }
	loading.value = true;
	try {
		const data = draft.value ? await draftState(conversation) : await Api.conversationExtensions(conversation);
		if (current === generation) state.value = data;
	} catch (e) {
		if (current === generation) error.value = e?.response?.data?.message || "扩展状态读取失败";
	} finally {
		if (current === generation) loading.value = false;
	}
}
async function draftState(conversation) {
	const data = await Api.folderExtensions(props.folderId || "__temporary");
	const chosen = extensionDraft(conversation) || {enabled: data.default?.enabled || [], autoAllow: Boolean(data.default?.autoAllow)};
	return {autoAllow: chosen.autoAllow, extensions: (data.extensions || []).map(item => ({...item, enabled: chosen.enabled.includes(item.key)}))};
}
function changeDraft(body) {
	const current = state.value;
	if (!current) return;
	const on = new Set(current.extensions.filter(item => item.enabled).map(item => item.key));
	const requires = Object.fromEntries(current.extensions.map(item => [item.key, item.requires || []]));
	// As the server stores it: an extension comes with what it requires and goes with it.
	for (const key of body.enable || []) [key, ...requires[key] || []].forEach(k => on.add(k));
	for (const key of body.disable || []) {
		on.delete(key);
		for (const [other, needs] of Object.entries(requires)) if (needs.includes(key)) on.delete(other);
	}
	const next = {autoAllow: typeof body.autoAllow === "boolean" ? body.autoAllow : current.autoAllow,
		extensions: current.extensions.map(item => ({...item, enabled: on.has(item.key)}))};
	setExtensionDraft(props.conversationUuid, {enabled: [...on], autoAllow: next.autoAllow});
	state.value = next;
}
async function change(body, key) {
	if (!canChange.value) return;
	if (draft.value) return changeDraft(body);
	const conversation = props.conversationUuid, current = ++generation;
	loading.value = false;
	saving.value = key; error.value = "";
	try {
		const data = await Api.updateConversationExtensions(conversation, body);
		if (current === generation) state.value = data;
	} catch (e) {
		if (current === generation) error.value = e?.response?.data?.message || "保存失败";
	} finally {
		if (current === generation) saving.value = "";
	}
}
const toggle = (item, on) => change(on ? {enable: [item.key]} : {disable: [item.key]}, item.key);
const setAutoAllow = on => change({autoAllow: on}, "autoAllow");

function updateMedia() { phone.value = Boolean(media?.matches); }
onMounted(() => { media = window.matchMedia?.("(max-width: 760px)") || null; updateMedia(); media?.addEventListener("change", updateMedia); load(); });
const stopDraftListener = onExtensionDraftForgotten(uuid => {
	if (draft.value && uuid === props.conversationUuid) { resetState(); void load(); }
});
onBeforeUnmount(() => { stopDraftListener(); media?.removeEventListener("change", updateMedia); });
watch(identity, () => { resetState(); void load(); });
watch(() => props.conversationUuid, (now, before) => {
	// The draft became a real conversation: its panel stays as it is and now shows the stored state.
	if (!String(before || "").startsWith("local:")) open.value = false;
});
watch(open, value => { if (value) load(); });
// The model may turn one on during a run.
watch(() => props.running, (now, before) => { if (before && !now) load(); });

const sheetOffset = ref(0), sheetDragging = ref(false), sheetClosing = ref(false);
let gesture = null;
const sheetStyle = computed(() => sheetOffset.value
	? {"--ext-drag-y": `${sheetOffset.value}px`, transform: sheetClosing.value ? undefined : `translateY(${sheetOffset.value}px)`}
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
	<template v-if="usable">
		<el-popover v-if="!phone" v-model:visible="open" trigger="click" placement="top-start" :show-arrow="false" :hide-after="0"
		            popper-class="composer-menu-popper extensions-menu-popper" :width="'min(24rem, calc(100vw - 2rem))'">
			<template #reference>
				<span class="ext-trigger"><slot name="trigger" :label="label" :open="open"/></span>
			</template>
			<div class="ext-panel" role="dialog" aria-label="扩展">
				<div class="ext-head"><span class="ext-title">扩展</span><span class="ext-note">{{ note }}</span></div>
				<div v-if="loading && !state" class="ext-empty">正在读取…</div>
				<ul v-else class="ext-list">
					<li v-for="item in items" :key="item.key" class="ext-row">
						<div class="ext-text"><span class="ext-label">{{ item.label }}</span><span class="ext-hint" :title="item.hint">{{ item.hint }}</span></div>
						<span class="ext-tokens" :title="`开启后每次请求约多 ${item.tokens} token`">≈{{ fmtTokens(item.tokens) }}</span>
						<el-switch :model-value="item.enabled" size="small" :loading="saving === item.key" :disabled="!canChange" :aria-label="`${item.label}扩展`" @update:model-value="toggle(item, $event)"/>
					</li>
				</ul>
				<div class="ext-auto">
					<div class="ext-text"><span class="ext-label">自动允许模型开启扩展</span><span class="ext-hint">模型申请时直接开启，不再问你</span></div>
					<el-switch :model-value="Boolean(state?.autoAllow)" size="small" :loading="saving === 'autoAllow'" :disabled="!canChange" aria-label="自动允许模型开启扩展" @update:model-value="setAutoAllow"/>
				</div>
				<p v-if="error" class="ext-error" role="alert">{{ error }}</p>
				<p class="ext-foot">每次开关后，下一次请求要重新写入一次缓存。</p>
			</div>
		</el-popover>
		<template v-else>
			<span class="ext-trigger" @click="open = true"><slot name="trigger" :label="label" :open="open"/></span>
			<el-drawer v-model="open" direction="btt" size="auto" append-to-body :with-header="false"
			           :class="['extensions-sheet', 'mobile-viewport-sheet', {'is-dragging': sheetDragging}]" :style="sheetStyle"
			           modal-class="extensions-overlay" aria-label="扩展" @closed="sheetClosed">
				<div class="ext-panel">
					<header class="ext-head ext-sheet-head" @pointerdown="dragStart" @pointermove="dragMove" @pointerup="dragEnd" @pointercancel="dragCancel">
						<div class="ext-grip" aria-hidden="true"></div>
						<span class="ext-title">扩展</span><span class="ext-note">{{ note }}</span>
						<button type="button" class="ext-close" aria-label="关闭扩展" @click="open = false">×</button>
					</header>
					<div v-if="loading && !state" class="ext-empty">正在读取…</div>
					<ul v-else class="ext-list">
						<li v-for="item in items" :key="item.key" class="ext-row">
							<div class="ext-text"><span class="ext-label">{{ item.label }}</span><span class="ext-hint">{{ item.hint }}</span></div>
							<span class="ext-tokens">≈{{ fmtTokens(item.tokens) }}</span>
							<el-switch :model-value="item.enabled" :loading="saving === item.key" :disabled="!canChange" :aria-label="`${item.label}扩展`" @update:model-value="toggle(item, $event)"/>
						</li>
					</ul>
					<div class="ext-auto">
						<div class="ext-text"><span class="ext-label">自动允许模型开启扩展</span><span class="ext-hint">模型申请时直接开启，不再问你</span></div>
						<el-switch :model-value="Boolean(state?.autoAllow)" :loading="saving === 'autoAllow'" :disabled="!canChange" aria-label="自动允许模型开启扩展" @update:model-value="setAutoAllow"/>
					</div>
					<p v-if="error" class="ext-error" role="alert">{{ error }}</p>
					<p class="ext-foot">每次开关后，下一次请求要重新写入一次缓存。</p>
				</div>
			</el-drawer>
		</template>
	</template>
</template>

<style>
.ext-trigger { display: inline-flex; }
.extensions-menu-popper.el-popper { padding: 0; }
.ext-panel { display: flex; flex-direction: column; min-width: 0; }
.ext-head { display: flex; align-items: baseline; gap: .5rem; padding: .6rem .75rem .35rem; }
.ext-title { font-size: 13px; font-weight: 600; color: var(--ob-text-strong); }
.ext-note { min-width: 0; font-size: 11.5px; color: var(--ob-text-muted); }
.ext-list { margin: 0; padding: 0 .25rem; list-style: none; max-height: min(22rem, 55vh); overflow-y: auto; }
.ext-row, .ext-auto { display: flex; align-items: center; gap: .6rem; min-height: 40px; padding: .35rem .5rem; border-radius: 8px; }
.ext-row:hover { background: var(--ob-hover); }
.ext-text { display: flex; flex: 1; flex-direction: column; min-width: 0; }
.ext-label { font-size: 13px; color: var(--ob-text); }
.ext-hint { overflow: hidden; font-size: 11.5px; line-height: 1.45; color: var(--ob-text-muted); text-overflow: ellipsis; white-space: nowrap; }
.ext-tokens { flex: none; font-size: 11.5px; color: var(--ob-text-subtle); font-variant-numeric: tabular-nums; }
.ext-auto { margin: .25rem .25rem 0; border-top: 1px solid var(--ob-border-soft); border-radius: 0; padding-top: .5rem; }
.ext-error { margin: .35rem .75rem 0; font-size: 12px; color: var(--ob-danger); }
.ext-foot { margin: .25rem .75rem .6rem; font-size: 11px; color: var(--ob-text-muted); }
.ext-empty { padding: .75rem; font-size: 12px; color: var(--ob-text-muted); }
.ext-close { display: none; }
@media (max-width: 760px) {
	.extensions-sheet.el-drawer.btt { border-radius: 16px 16px 0 0; background: var(--ob-surface-raised); transition: transform 280ms cubic-bezier(.22, .68, 0, 1); }
	html .extensions-sheet.mobile-viewport-sheet > .el-drawer__body { padding: 0 0 max(10px, env(safe-area-inset-bottom, 0px)); }
	.extensions-sheet.is-dragging { transition: none; user-select: none; }
	.extensions-overlay.el-drawer-fade-leave-active { transition: background-color 280ms linear; }
	.extensions-overlay.el-drawer-fade-leave-from .extensions-sheet { transform: translateY(var(--ext-drag-y, 0px)); }
	.extensions-overlay.el-drawer-fade-leave-active .extensions-sheet { pointer-events: none; }
	.ext-sheet-head { position: relative; align-items: center; padding: 20px max(16px, env(safe-area-inset-right, 0px)) 6px max(16px, env(safe-area-inset-left, 0px)); touch-action: none; }
	.ext-grip { position: absolute; top: 8px; left: calc(50% - 15px); width: 30px; height: 4px; border-radius: 2px; background: var(--ob-border-strong); }
	.ext-title { font-size: 15px; }
	.ext-note { flex: 1; font-size: 12px; }
	.ext-close { display: grid; place-items: center; width: 36px; height: 36px; margin: -6px -8px -6px 0; border: 0; border-radius: 999px; background: transparent; color: var(--ob-text-subtle); font-size: 20px; }
	.ext-list { max-height: calc(var(--mobile-viewport-height, 100dvh) * .62); padding: 0 max(8px, env(safe-area-inset-right, 0px)) 0 max(8px, env(safe-area-inset-left, 0px)); }
	.ext-row, .ext-auto { min-height: 52px; padding: .4rem .6rem; }
	.ext-label { font-size: 14px; }
	.ext-hint { font-size: 12px; white-space: normal; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
	.ext-auto { margin: .25rem max(8px, env(safe-area-inset-right, 0px)) 0 max(8px, env(safe-area-inset-left, 0px)); }
	.ext-foot, .ext-error { margin-left: max(16px, env(safe-area-inset-left, 0px)); margin-right: max(16px, env(safe-area-inset-right, 0px)); }
}
</style>
