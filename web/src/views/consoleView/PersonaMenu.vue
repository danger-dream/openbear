<script setup>
import {computed, onBeforeUnmount, onMounted, ref, watch} from "vue";
import BearLogo from "../../components/BearLogo.vue";
import {loadPromptTemplateChoices, normalizePromptTemplate, promptTemplateChoices, promptTemplateInitial, promptTemplateLabel, promptTemplateMeta, promptTemplateName, promptTemplateTint} from "./promptTemplate.js";

// The composer's 人设 choice: which system prompt template this conversation renders.
// Chosen before the first round; afterwards the list only shows the choice. A desktop
// popover; on a phone a bottom sheet whose header follows a downward drag, as the
// extensions one does. The composer renders the trigger and owns the setting.
const props = defineProps({
	value: {type: String, default: ""},
	locked: {type: Boolean, default: false},
	missing: {type: Boolean, default: false},
	saving: {type: Boolean, default: false},
	draft: {type: Boolean, default: false},
	conversationUuid: {type: String, default: ""},
});
const emit = defineEmits(["change"]);

const open = ref(false), phone = ref(false), loading = ref(false), error = ref("");
const selected = computed(() => normalizePromptTemplate(props.value));
const attention = computed(() => props.missing || Boolean(selected.value && promptTemplateChoices.existingIds && !promptTemplateChoices.existingIds.includes(selected.value)));
const label = computed(() => promptTemplateLabel(selected.value) + (attention.value ? "；模板已删除，重组上下文时使用系统默认" : ""));
const summary = computed(() => selected.value ? promptTemplateName(selected.value) : "系统默认");
// The phone composer lists this menu in its tools sheet and opens it from there.
defineExpose({show: () => { open.value = true; }, summary, attention});
const now = ref(Date.now());
const rows = computed(() => {
	const active = promptTemplateChoices.active;
	const persona = (id, name, hint) => ({id, name, hint, initial: promptTemplateInitial(name), tint: promptTemplateTint(id)});
	return [
		{id: "", name: "系统默认", hint: [active.name, promptTemplateMeta(active, now.value)].filter(Boolean).join(" · "), logo: true},
		...promptTemplateChoices.items.map(item => persona(item.id, item.name, promptTemplateMeta(item, now.value))),
		...(selected.value && !promptTemplateChoices.items.some(item => item.id === selected.value)
			? [persona(selected.value, promptTemplateName(selected.value), selected.value === active.id ? "已选定此模板 · 当前系统默认" : attention.value ? "模板已删除 · 重组时使用系统默认" : "已不再标记为人设")] : []),
	];
});
const note = computed(() => props.locked ? "会话已开始，不能更改" : props.draft ? "发送第一条消息时生效" : "第一轮开始前可以更改");
function choose(row) {
	if (props.locked || props.saving || row.id === selected.value) return;
	emit("change", row.id);
}

async function refresh() {
	loading.value = !promptTemplateChoices.loaded; error.value = "";
	try { await loadPromptTemplateChoices({force: true}); }
	catch { error.value = "人设列表读取失败"; }
	finally { loading.value = false; }
}
watch(open, opened => { if (opened) { now.value = Date.now(); refresh(); } });

let media = null;
function updateMedia() { phone.value = Boolean(media?.matches); }
onMounted(() => {
	media = window.matchMedia?.("(max-width: 760px)") || null; updateMedia(); media?.addEventListener("change", updateMedia);
	loadPromptTemplateChoices().catch(() => {});
});
onBeforeUnmount(() => { media?.removeEventListener("change", updateMedia); });
watch(() => props.conversationUuid, (now, before) => {
	if (!String(before || "").startsWith("local:")) open.value = false;
});

const sheetOffset = ref(0), sheetDragging = ref(false), sheetClosing = ref(false);
let gesture = null;
const sheetStyle = computed(() => sheetOffset.value
	? {"--persona-drag-y": `${sheetOffset.value}px`, transform: sheetClosing.value ? undefined : `translateY(${sheetOffset.value}px)`}
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
	<el-popover v-if="!phone" v-model:visible="open" trigger="click" placement="top-start" :show-arrow="false" :hide-after="0"
	            popper-class="composer-menu-popper persona-menu-popper" :width="'min(23rem, calc(100vw - 2rem))'">
		<template #reference>
			<span class="persona-trigger"><slot name="trigger" :label="label" :open="open" :on="Boolean(selected)" :attention="attention"/></span>
		</template>
		<div class="persona-panel" role="dialog" aria-label="人设">
			<div class="persona-head"><span class="persona-title">人设</span><span class="persona-note">{{ note }}</span></div>
			<div v-if="loading" class="persona-empty">正在读取…</div>
			<ul v-else class="persona-list" role="listbox" aria-label="人设" :aria-disabled="locked">
				<li v-for="row in rows" :key="row.id" role="option" class="persona-row" :aria-selected="row.id === selected"
				    :class="{'is-selected': row.id === selected, 'is-locked': locked}" :tabindex="locked ? -1 : 0"
				    @click="choose(row)" @keydown.enter.prevent="choose(row)" @keydown.space.prevent="choose(row)">
					<span class="persona-avatar" :class="row.logo ? 'is-logo' : `is-${row.tint}`" aria-hidden="true"><BearLogo v-if="row.logo"/><template v-else>{{ row.initial }}</template></span>
					<div class="persona-text"><span class="persona-label">{{ row.name }}</span><span v-if="row.hint" class="persona-hint">{{ row.hint }}</span></div>
					<svg v-if="row.id === selected" class="persona-check" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>
				</li>
			</ul>
			<p v-if="error" class="persona-error" role="alert">{{ error }}</p>
			<p class="persona-foot">在 设置 → 提示词模板 里打开「作为人设」即可出现在这里。勾选了「覆盖系统提示词」时以覆盖内容为准。</p>
		</div>
	</el-popover>
	<template v-else>
		<span class="persona-trigger" @click="open = true"><slot name="trigger" :label="label" :open="open" :on="Boolean(selected)" :attention="attention"/></span>
		<el-drawer v-model="open" direction="btt" size="auto" append-to-body :with-header="false"
		           :class="['persona-sheet', 'mobile-viewport-sheet', {'is-dragging': sheetDragging}]" :style="sheetStyle"
		           modal-class="persona-overlay" aria-label="人设" @closed="sheetClosed">
			<div class="persona-panel">
				<header class="persona-head persona-sheet-head" @pointerdown="dragStart" @pointermove="dragMove" @pointerup="dragEnd" @pointercancel="dragCancel">
					<div class="persona-grip" aria-hidden="true"></div>
					<span class="persona-title">人设</span><span class="persona-note">{{ note }}</span>
					<button type="button" class="persona-close" aria-label="关闭人设" @click="open = false">×</button>
				</header>
				<div v-if="loading" class="persona-empty">正在读取…</div>
				<ul v-else class="persona-list" role="listbox" aria-label="人设" :aria-disabled="locked">
					<li v-for="row in rows" :key="row.id" role="option" class="persona-row" :aria-selected="row.id === selected"
					    :class="{'is-selected': row.id === selected, 'is-locked': locked}" @click="choose(row)">
						<span class="persona-avatar" :class="row.logo ? 'is-logo' : `is-${row.tint}`" aria-hidden="true"><BearLogo v-if="row.logo"/><template v-else>{{ row.initial }}</template></span>
					<div class="persona-text"><span class="persona-label">{{ row.name }}</span><span v-if="row.hint" class="persona-hint">{{ row.hint }}</span></div>
						<svg v-if="row.id === selected" class="persona-check" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>
					</li>
				</ul>
				<p v-if="error" class="persona-error" role="alert">{{ error }}</p>
				<p class="persona-foot">在 设置 → 提示词模板 里打开「作为人设」即可出现在这里。勾选了「覆盖系统提示词」时以覆盖内容为准。</p>
			</div>
		</el-drawer>
	</template>
</template>

<style>
.persona-trigger { display: inline-flex; }
.persona-menu-popper.el-popper { padding: 0; }
.persona-panel { display: flex; flex-direction: column; min-width: 0; }
.persona-head { display: flex; align-items: baseline; gap: .5rem; padding: .6rem .75rem .35rem; }
.persona-title { font-size: 13px; font-weight: 600; color: var(--ob-text-strong); }
.persona-note { min-width: 0; font-size: 11.5px; color: var(--ob-text-muted); }
.persona-list { margin: 0; padding: 0 .25rem; list-style: none; max-height: min(22rem, 55vh); overflow-y: auto; }
.persona-row { display: flex; align-items: center; gap: .65rem; min-height: 48px; padding: .4rem .5rem; border-radius: 8px; cursor: pointer; outline: none; }
.persona-row:hover, .persona-row:focus-visible { background: var(--ob-hover); }
.persona-row.is-selected { background: var(--ob-selected); }
.persona-row.is-locked { cursor: default; }
.persona-row.is-locked:not(.is-selected):hover { background: transparent; }
.persona-row.is-locked:not(.is-selected) > * { opacity: .5; }
.persona-avatar { display: grid; flex: none; place-items: center; width: 30px; height: 30px; overflow: hidden; border-radius: 8px; font-size: 13px; font-weight: 600; line-height: 1; --persona-tint: var(--ob-blue-rgb); background: rgb(var(--persona-tint) / .12); color: rgb(var(--persona-tint)); }
.persona-avatar.is-violet { --persona-tint: var(--ob-violet-rgb); }
.persona-avatar.is-success { --persona-tint: var(--ob-success-rgb); }
.persona-avatar.is-orange { --persona-tint: var(--ob-orange-rgb); }
.persona-avatar.is-danger { --persona-tint: var(--ob-danger-rgb); }
.persona-avatar.is-info { --persona-tint: var(--ob-info-rgb); }
.persona-avatar.is-logo { background: transparent; }
.persona-text { display: flex; flex: 1; flex-direction: column; gap: 1px; min-width: 0; }
.persona-label { overflow: hidden; font-size: 13px; font-weight: 500; color: var(--ob-text); text-overflow: ellipsis; white-space: nowrap; }
.persona-row.is-selected .persona-label { color: var(--ob-blue); }
.persona-hint { overflow: hidden; font-size: 11.5px; line-height: 1.45; color: var(--ob-text-muted); text-overflow: ellipsis; white-space: nowrap; }
.persona-check { flex: none; width: 14px; height: 14px; fill: none; stroke: var(--ob-blue); stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
.persona-empty { padding: .75rem; font-size: 12px; color: var(--ob-text-muted); }
.persona-error { margin: .35rem .75rem 0; font-size: 12px; color: var(--ob-danger); }
.persona-foot { margin: .25rem .75rem .6rem; padding-top: .45rem; border-top: 1px solid var(--ob-border-soft); font-size: 11px; line-height: 1.55; color: var(--ob-text-muted); }
.persona-close { display: none; }
@media (max-width: 760px) {
	.persona-sheet.el-drawer.btt { border-radius: 16px 16px 0 0; background: var(--ob-surface-raised); transition: transform 280ms cubic-bezier(.22, .68, 0, 1); }
	html .persona-sheet.mobile-viewport-sheet > .el-drawer__body { padding: 0 0 max(10px, env(safe-area-inset-bottom, 0px)); }
	.persona-sheet.is-dragging { transition: none; user-select: none; }
	.persona-overlay.el-drawer-fade-leave-active { transition: background-color 280ms linear; }
	.persona-overlay.el-drawer-fade-leave-from .persona-sheet { transform: translateY(var(--persona-drag-y, 0px)); }
	.persona-overlay.el-drawer-fade-leave-active .persona-sheet { pointer-events: none; }
	.persona-sheet-head { position: relative; align-items: center; padding: 20px max(16px, env(safe-area-inset-right, 0px)) 6px max(16px, env(safe-area-inset-left, 0px)); touch-action: none; }
	.persona-grip { position: absolute; top: 8px; left: calc(50% - 15px); width: 30px; height: 4px; border-radius: 2px; background: var(--ob-border-strong); }
	.persona-title { font-size: 15px; }
	.persona-note { flex: 1; font-size: 12px; }
	.persona-close { display: grid; place-items: center; width: 36px; height: 36px; margin: -6px -8px -6px 0; border: 0; border-radius: 999px; background: transparent; color: var(--ob-text-subtle); font-size: 20px; }
	.persona-list { max-height: calc(var(--mobile-viewport-height, 100dvh) * .62); padding: 0 max(8px, env(safe-area-inset-right, 0px)) 0 max(8px, env(safe-area-inset-left, 0px)); }
	.persona-row { min-height: 56px; padding: .45rem .6rem; }
	.persona-avatar { width: 34px; height: 34px; font-size: 14px; }
	.persona-label { font-size: 14px; }
	.persona-hint { font-size: 12px; }
	.persona-foot { margin: .35rem max(16px, env(safe-area-inset-right, 0px)) .4rem max(16px, env(safe-area-inset-left, 0px)); font-size: 12px; }
}
</style>
