<script setup>
import {nextTick, onBeforeUnmount, onMounted, ref, watch} from "vue";
import {ChatLineRound, CollectionTag, DataAnalysis, Lock, Money, MoreFilled, Timer, Unlock} from "@element-plus/icons-vue";
import WorkDetailIcon from "./WorkDetailIcon.vue";
import {cachePct, fmtTokens, shortText} from "./display.js";

const props = defineProps({
	conversationUuid: {type: String, default: ""},
	turns: {type: Array, default: () => []},
	activeTurnIndex: {type: Number, default: 0},
	canOpenWork: {type: Boolean, default: false},
	workOpen: {type: Boolean, default: false},
	autoScrollLocked: {type: Boolean, default: true},
	tokenParts: {type: Object, default: () => ({})},
	durationText: {type: String, default: "0s"},
	costText: {type: String, default: "$0.0000"},
});
const emit = defineEmits(["open-memory", "toggle-work", "toggle-scroll-lock", "scroll-to-turn"]);
const phone = ref(false);
const menuOpen = ref(false);
const navigationOpen = ref(false);
const navigationList = ref(null);
let media;
function closePanels() { menuOpen.value = false; navigationOpen.value = false; }
function updateMedia() { phone.value = Boolean(media?.matches); if (!phone.value) closePanels(); }
onMounted(() => { media = window.matchMedia("(max-width: 760px)"); updateMedia(); media.addEventListener("change", updateMedia); });
onBeforeUnmount(() => media?.removeEventListener("change", updateMedia));
watch(() => props.conversationUuid, closePanels);
async function chooseAction(action) {
	const conversationUuid = props.conversationUuid;
	menuOpen.value = false;
	await nextTick();
	if (conversationUuid !== props.conversationUuid || !phone.value) return;
	emit(action);
}
function openNavigation() { menuOpen.value = false; navigationOpen.value = true; }
function chooseTurn(index) { navigationOpen.value = false; emit("scroll-to-turn", index); }
function revealCurrentTurn() {
	const list = navigationList.value;
	const active = list?.querySelector("[aria-current='true']");
	if (active) list.scrollTop = Math.max(0, active.offsetTop - list.clientHeight / 2 + active.offsetHeight / 2);
}
function turnLabel(turn, index) { return shortText(turn?.user?.content || "", 80) || `第 ${index + 1} 轮对话`; }
</script>

<template>
	<div v-if="phone" class="mobile-conversation-tools">
		<el-popover v-model:visible="menuOpen" trigger="click" placement="bottom-end" :width="224" :show-arrow="false" popper-class="mobile-conversation-menu">
			<template #reference>
				<button type="button" class="mobile-more-button" aria-label="更多会话操作" aria-haspopup="dialog" :aria-expanded="menuOpen"><MoreFilled/></button>
			</template>
			<section class="mobile-conversation-summary" aria-label="会话统计">
				<div class="mobile-summary-caption">会话统计</div>
				<dl class="mobile-summary-metrics">
					<div class="mobile-summary-token-group">
						<dt><DataAnalysis aria-hidden="true"/><span>总 Tokens</span></dt>
						<dd class="mobile-token-lines">
							<div class="mobile-token-row"><span class="mobile-token-label">输入</span><span class="mobile-token-value">{{ fmtTokens(props.tokenParts.input) }}</span></div>
							<div class="mobile-token-row"><span class="mobile-token-label">输出</span><span class="mobile-token-value">{{ fmtTokens(props.tokenParts.output) }}</span></div>
							<div class="mobile-token-row"><span class="mobile-token-label">缓存</span><span class="mobile-token-value">{{ fmtTokens(props.tokenParts.cache) }}（{{ cachePct(props.tokenParts.cache, props.tokenParts.input) }}）</span></div>
						</dd>
					</div>
					<div><dt><Timer aria-hidden="true"/><span>总耗时</span></dt><dd>{{ props.durationText }}</dd></div>
					<div><dt><Money aria-hidden="true"/><span>总花费</span></dt><dd>{{ props.costText }}</dd></div>
				</dl>
			</section>
			<div class="mobile-tool-actions">
				<button type="button" :disabled="!props.conversationUuid || props.conversationUuid.startsWith('local:')" @click="chooseAction('open-memory')"><CollectionTag/><span>任务记忆</span></button>
				<button type="button" :disabled="!props.canOpenWork" @click="chooseAction('toggle-work')"><WorkDetailIcon/><span>{{ props.workOpen ? '关闭工作详情' : '工作详情' }}</span></button>
				<button type="button" :disabled="!props.turns.length" @click="openNavigation"><ChatLineRound/><span>对话导航</span><small v-if="props.turns.length">{{ props.activeTurnIndex + 1 }} / {{ props.turns.length }}</small></button>
				<div class="mobile-menu-divider"></div>
				<button type="button" :aria-pressed="props.autoScrollLocked" @click="chooseAction('toggle-scroll-lock')"><Lock v-if="props.autoScrollLocked"/><Unlock v-else/><span>跟随最新消息</span><small>{{ props.autoScrollLocked ? '开启' : '关闭' }}</small></button>
			</div>
		</el-popover>
		<el-drawer v-model="navigationOpen" direction="btt" size="min(60dvh, 30rem)" title="对话导航" class="mobile-turn-navigation" append-to-body @opened="revealCurrentTurn">
			<nav ref="navigationList" class="mobile-turn-list" aria-label="选择对话轮次">
				<button v-for="(turn, index) in props.turns" :key="turn.id || index" type="button" class="mobile-turn-row" :aria-current="props.activeTurnIndex === index ? 'true' : undefined" @click="chooseTurn(index)">
					<span class="mobile-turn-index">{{ index + 1 }}</span><span>{{ turnLabel(turn, index) }}</span>
				</button>
			</nav>
		</el-drawer>
	</div>
</template>

<style scoped>
.mobile-conversation-tools { display: flex; flex: 0 0 44px; }
.mobile-more-button { display: grid; place-items: center; width: 44px; height: 44px; padding: 0; appearance: none; border: 0; border-radius: 10px; background: transparent; color: var(--bear-ink); cursor: pointer; }
.mobile-more-button svg { width: 19px; height: 19px; }
.mobile-conversation-summary { margin: 2px 2px 6px; padding: 10px; border-radius: 10px; background: var(--el-fill-color-light); }
.mobile-summary-caption { color: var(--el-text-color-secondary); font-size: 11px; font-weight: 500; line-height: 16px; }
.mobile-summary-metrics { display: grid; gap: 7px; margin: 8px 0 0; }
.mobile-summary-metrics > div { display: grid; grid-template-columns: max-content minmax(0, 1fr); align-items: baseline; gap: 10px; }
.mobile-summary-metrics dt { display: inline-flex; align-items: center; gap: 6px; color: var(--el-text-color-regular); font-size: 12px; line-height: 18px; white-space: nowrap; }
.mobile-summary-metrics dt svg { width: 13px; height: 13px; flex: none; color: var(--el-text-color-secondary); }
.mobile-summary-metrics dd { min-width: 0; margin: 0; color: var(--el-text-color-primary); font-size: 12px; font-weight: 600; line-height: 18px; font-variant-numeric: tabular-nums; text-align: right; overflow-wrap: anywhere; }
.mobile-summary-metrics > .mobile-summary-token-group { grid-template-columns: minmax(0, 1fr); gap: 5px; }
.mobile-token-lines { display: grid; gap: 4px; }
.mobile-token-row { display: grid; grid-template-columns: max-content minmax(0, 1fr); align-items: baseline; gap: 10px; }
.mobile-token-label { color: var(--el-text-color-secondary); font-weight: 400; text-align: left; white-space: nowrap; }
.mobile-token-value { white-space: nowrap; }
.mobile-tool-actions { display: grid; gap: 2px; }
.mobile-tool-actions button { display: flex; align-items: center; gap: 10px; width: 100%; min-height: 44px; padding: 0 10px; border: 0; border-radius: 8px; background: transparent; color: var(--el-text-color-primary); text-align: left; font-size: 13px; cursor: pointer; }
.mobile-tool-actions button:hover:not(:disabled) { background: var(--el-fill-color-light); }
.mobile-tool-actions button:disabled { opacity: .4; cursor: default; }
.mobile-tool-actions svg { width: 17px; height: 17px; flex: none; color: var(--el-text-color-secondary); }
.mobile-tool-actions small { margin-left: auto; color: var(--el-text-color-secondary); font-size: 11px; }
.mobile-menu-divider { height: 1px; margin: 4px 10px; background: var(--el-border-color-lighter); }
.mobile-turn-list { position: relative; height: 100%; min-height: 0; overflow-y: auto; overscroll-behavior: contain; }
.mobile-turn-row { display: grid; grid-template-columns: 24px minmax(0, 1fr); gap: 12px; align-items: center; width: 100%; min-height: 48px; padding: 10px; border: 0; border-radius: 10px; background: transparent; color: var(--el-text-color-primary); text-align: left; font-size: 13px; line-height: 1.5; }
.mobile-turn-row > span:last-child { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }
.mobile-turn-index { font-size: 11px; color: var(--el-text-color-secondary); }
.mobile-turn-row[aria-current='true'] { background: var(--el-fill-color-light); }
.mobile-turn-row[aria-current='true'] .mobile-turn-index { color: var(--el-color-primary); }
button:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: -2px; }
</style>

<style>
.mobile-conversation-menu.el-popper { padding: 6px; border-radius: 14px; max-width: calc(100vw - 24px); max-height: calc(var(--mobile-viewport-height, 100dvh) - 64px - env(safe-area-inset-top, 0px)); overflow-y: auto; overscroll-behavior: contain; }
.mobile-turn-navigation.el-drawer { border-radius: 18px 18px 0 0; }
.mobile-turn-navigation .el-drawer__header { margin: 0; padding: 8px 16px; align-items: center; }
.mobile-turn-navigation .el-drawer__title { font-size: 14px; font-weight: 600; }
.mobile-turn-navigation .el-drawer__close-btn { width: 44px; height: 44px; padding: 0; }
.mobile-turn-navigation .el-drawer__body { overflow: hidden; padding: 0 12px max(12px, env(safe-area-inset-bottom, 0px)); }
</style>
