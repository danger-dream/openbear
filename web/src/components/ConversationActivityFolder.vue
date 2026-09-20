<script setup>
import {computed, onBeforeUnmount, onMounted, ref, watch} from "vue";
import {ArrowRight, Check, Folder, FolderOpened} from "@element-plus/icons-vue";
import {activityLabel, activityState} from "../conversationActivity.js";
const props = defineProps({
  items: {type: Array, default: () => []},
  recentItems: {type: Array, default: () => []},
  activeConversationUuid: {type: String, default: ""},
  readVersions: {type: Map, default: () => new Map()},
  busy: Boolean,
});
const emit = defineEmits(["open", "read", "read-all", "overview-enter", "overview-leave", "overview-close"]);
const expanded = ref(true);
const clock = ref(Date.now());
let clockTimer;
onMounted(() => { clockTimer = setInterval(() => { clock.value = Date.now(); }, 60000); });
onBeforeUnmount(() => clearInterval(clockTimer));
function recentInteractionTime(value, now = clock.value) {
  const elapsed = Math.max(0, now - Number(value || 0));
  if (elapsed < 60000) return "刚刚";
  if (elapsed < 3600000) return `${Math.floor(elapsed / 60000)} 分钟前`;
  if (elapsed < 86400000) return `${Math.floor(elapsed / 3600000)} 小时前`;
  return `${Math.floor(elapsed / 86400000)} 天前`;
}
function interactionAt(item) { return Number(item.lastInteractionAtMs || item.activityAtMs || 0); }
function rowState(item) {
  // A read receipt is not a successful result and cannot clear pending work.
  return item.activityPending?.length ? "waiting" : activityState({...item, readWhileSelected: false});
}
function statusLabel(item) {
  const state = rowState(item);
  const label = activityLabel({...item, activityState: state, readWhileSelected: false});
  if (state === "waiting" || state === "running") return label;
  const settled = ["completed", "idle", "read"].includes(state);
  if (item.activityUnread) return settled ? "完成待查看" : `${label}待查看`;
  // Failures, interruptions and partial/cancelled results remain explicit even
  // after reading. Only an ordinary settled, read conversation uses time.
  return settled ? "" : label;
}
function rowLabel(item) {
  return statusLabel(item) || (interactionAt(item) > 0 ? recentInteractionTime(interactionAt(item)) : activityLabel(item));
}
function rowTitle(item) {
  return statusLabel(item) || (interactionAt(item) > 0 ? `最后交互：${new Date(interactionAt(item)).toLocaleString()}` : activityLabel(item));
}
const selectedRow = ref(null);
watch(() => [props.items, props.activeConversationUuid, props.readVersions], () => {
  const current = props.items.find(item => item.conversationUuid === props.activeConversationUuid);
  if (current) selectedRow.value = current;
  else if (selectedRow.value?.conversationUuid !== props.activeConversationUuid
    || !selectedRow.value?.activityVersion
    || Number(props.readVersions.get(props.activeConversationUuid) || 0) < selectedRow.value.activityVersion) selectedRow.value = null;
}, {immediate: true});
const visibleItems = computed(() => {
  const items = [...props.items];
  if (selectedRow.value && !items.some(item => item.conversationUuid === selectedRow.value.conversationUuid)) {
    items.push({...selectedRow.value, running: false, activityUnread: false, readWhileSelected: true});
  }
  return items;
});
const recent = computed(() => [...new Map((props.recentItems || [])
  .filter(item => item?.conversationUuid && !item.archived && !item.local && Number(item.lastInteractionAtMs) > 0)
  .map(item => [item.conversationUuid, item])).values()]
  .sort((a, b) => Number(b.lastInteractionAtMs) - Number(a.lastInteractionAtMs) || a.conversationUuid.localeCompare(b.conversationUuid))
  .slice(0, 5));
const rows = computed(() => {
  const unique = new Map(recent.value.map(item => [item.conversationUuid, item]));
  for (const item of visibleItems.value) {
    if (!item?.conversationUuid || item.archived || item.local) continue;
    const current = unique.get(item.conversationUuid);
    // Current live status wins; a selected-row retention snapshot must never
    // overwrite a newer recent result or create a second row for it.
    if (!current || !item.readWhileSelected) unique.set(item.conversationUuid, {...current, ...item});
  }
  // The five-item limit applies to ordinary recents, not outstanding work.
  return [...unique.values()].sort((a, b) => interactionAt(b) - interactionAt(a)
    || a.conversationUuid.localeCompare(b.conversationUuid));
});
const unreadCount = computed(() => rows.value.filter(item => item.activityUnread).length);
const waitingCount = computed(() => rows.value.filter(item => rowState(item) === "waiting").length);
</script>

<template>
  <section class="activity-folder" aria-label="最近会话">
    <div class="activity-folder-heading">
      <button class="activity-folder-toggle" type="button" :aria-expanded="expanded" @click="expanded = !expanded; emit('overview-close')">
        <ArrowRight class="activity-chevron" :class="{'is-expanded': expanded}" />
        <component :is="expanded ? FolderOpened : Folder" class="activity-folder-icon" />
        <span class="activity-heading-label">最近会话</span>
        <span v-if="waitingCount" class="activity-waiting-count" :title="`${waitingCount} 个会话等待你处理`" role="status">待处理 {{ waitingCount }}</span>
        <span v-else class="activity-total">{{ rows.length }}</span>
      </button>
      <button v-if="unreadCount" class="activity-read-all" type="button" :disabled="busy" aria-label="全部标为已读" title="全部标为已读" @click="emit('read-all')"><Check /><span class="activity-touch-label">全部已读</span></button>
    </div>
    <div v-if="expanded" class="activity-folder-content" data-recent-conversations @scroll.passive="emit('overview-close')">
      <div v-for="item in rows" :key="item.conversationUuid" class="activity-row" :class="{'is-selected': item.conversationUuid === activeConversationUuid, 'is-waiting': rowState(item) === 'waiting'}" :data-activity-id="item.conversationUuid" @pointerenter="emit('overview-enter', $event, item)" @pointerleave="emit('overview-leave')">
        <button class="activity-row-open" type="button" :aria-label="`${item.title} · ${item.path || '临时会话'} · ${rowLabel(item)}`" @click="emit('open', item)">
          <i class="activity-state-dot" :class="`is-${rowState(item)}`" aria-hidden="true"></i>
          <span class="activity-row-copy">
            <span class="activity-row-title">{{ item.title }}</span>
            <span class="activity-row-status activity-row-status-touch" :aria-label="rowTitle(item)">{{ rowLabel(item) }}</span>
          </span>
          <span v-if="item.activityUnread" class="activity-unread-dot" aria-label="未读"></span>
        </button>
        <button v-if="item.activityUnread" class="activity-row-read" type="button" :disabled="busy" :aria-label="`标为已读：${item.title}`" @click="emit('read', item)"><Check /><span class="activity-touch-label">标已读</span></button>
        <span class="activity-row-status activity-row-status-desktop" :aria-label="rowTitle(item)" @click="emit('open', item)">{{ rowLabel(item) }}</span>
      </div>
      <p v-if="!rows.length" class="activity-empty">暂无最近会话</p>
    </div>
  </section>
</template>

<style scoped>
.activity-folder { display: flex; flex-direction: column; flex: 0 1 auto; min-height: 30px; max-height: 40%; margin: 0 7px 7px; border-bottom: 1px solid rgba(161,161,170,.22); padding-bottom: 5px; color: #52525b; font-size: 12px; }
.activity-folder button { font: inherit; color: inherit; border: 0; background: transparent; cursor: pointer; }
.activity-folder-heading { display: flex; flex: 0 0 auto; align-items: center; min-height: 29px; }
.activity-folder-toggle { display: flex; flex: 1; min-width: 0; align-items: center; gap: 6px; height: 29px; padding: 0 5px; text-align: left; border-radius: 7px; }
.activity-folder-toggle:hover, .activity-read-all:hover { background: rgba(228,228,231,.58); }
.activity-chevron { width: 12px; height: 12px; flex: 0 0 12px; color: #a1a1aa; }
.activity-chevron.is-expanded { transform: rotate(90deg); }
.activity-folder-icon { width: 15px; height: 15px; color: #7b8491; }
.activity-heading-label { white-space: nowrap; }
.activity-waiting-count { margin-left: auto; flex: 0 0 auto; padding: 2px 5px; border-radius: 5px; background: rgba(217,153,57,.12); color: #9a6419; font-size: 11px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.activity-row.is-waiting .activity-row-status { color: #9a6419; }
.activity-total { margin-left: auto; color: #a1a1aa; font-size: 11px; font-variant-numeric: tabular-nums; }
.activity-read-all, .activity-row-read { display: grid; flex: 0 0 24px; width: 24px; height: 25px; padding: 5px; place-items: center; border-radius: 5px; }
.activity-read-all svg, .activity-row-read svg { width: 13px; height: 13px; }
.activity-folder-content { flex: 0 1 auto; min-height: 0; max-height: min(32vh, 260px); overflow-y: auto; overscroll-behavior: contain; scrollbar-width: thin; }
.activity-row { display: flex; align-items: center; margin-left: 18px; border-radius: 7px; }
.activity-row:hover { background: rgba(228,228,231,.58); }
.activity-row.is-selected { background: rgba(37,99,235,.075); color: #1d4ed8; }
.activity-row-open { display: flex; gap: 6px; align-items: center; flex: 1; min-width: 0; height: 29px; padding: 0 5px; text-align: left; border-radius: 7px; }
.activity-row-copy { display: contents; }
.activity-touch-label { display: none; }
.activity-row-title { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.activity-row-status { flex: 0 0 auto; color: #8a8a95; font-size: 11px; }
.activity-row-status-desktop { margin: 0 5px 0 6px; cursor: pointer; }
.activity-row-status-touch { display: none; }
.activity-state-dot { box-sizing: border-box; flex: 0 0 8px; width: 8px; height: 8px; border: 1px solid #a1a1aa; border-radius: 50%; }
.activity-state-dot.is-running { border: 1.5px solid #b9cef7; border-top-color: #3b82f6; animation: activity-spin 1s linear infinite; }
.activity-state-dot.is-waiting, .activity-state-dot.is-partial { border-color: #d79939; background: #d79939; }
.activity-state-dot.is-failed, .activity-state-dot.is-error, .activity-state-dot.is-interrupted { border-color: #da6a67; background: #da6a67; }
.activity-state-dot.is-completed { border-color: #65a280; background: #65a280; }
.activity-state-dot.is-read { opacity: .45; }
.activity-unread-dot { flex: 0 0 5px; width: 5px; height: 5px; background: #3b82f6; border-radius: 50%; }
.activity-row-read { box-sizing: border-box; opacity: 1; color: #71717a !important; }
.activity-row-read:hover { background: rgba(161,161,170,.15) !important; }
.activity-folder button:disabled { opacity: .4; cursor: default; }
.activity-folder button:focus-visible { outline: 2px solid rgba(37,99,235,.5); outline-offset: -2px; }
.activity-empty { margin: 4px 8px 5px 26px; color: #a1a1aa; font-size: 11px; }
@keyframes activity-spin { to { transform: rotate(360deg); } }
/* Keep the running ring, like the ordinary tree: operational status, not decoration. */
@media (max-width: 760px), (pointer: coarse) {
  .activity-folder { max-height: 50%; margin: 0 7px 6px; font-size: 13px; }
  .activity-folder button { touch-action: manipulation; -webkit-tap-highlight-color: transparent; }
  .activity-folder-heading, .activity-folder-toggle { min-height: 44px; }
  .activity-folder-toggle { gap: 5px; height: 44px; }
  .activity-folder-icon { flex: 0 0 15px; }
  .activity-total { font-size: 12px; }
  .activity-waiting-count { padding: 2px 4px; font-size: 12px; }
  .activity-touch-label { display: inline; white-space: nowrap; font-size: 12px; }
  .activity-read-all { display: flex; flex: 0 0 auto; gap: 4px; width: auto; min-width: 44px; height: 44px; padding: 0 6px; align-items: center; justify-content: center; }
  .activity-folder-content { max-height: min(36vh, 300px); max-height: min(36dvh, 300px); overflow-x: hidden; -webkit-overflow-scrolling: touch; }
  .activity-row { margin-left: 0; align-items: stretch; }
  .activity-row-open { gap: 8px; height: auto; min-height: 56px; padding: 7px 6px; }
  .activity-row-copy { display: flex; flex: 1; min-width: 0; flex-direction: column; align-items: flex-start; gap: 2px; }
  .activity-row-title { display: -webkit-box; flex: none; width: 100%; white-space: normal; overflow-wrap: anywhere; -webkit-box-orient: vertical; -webkit-line-clamp: 2; line-height: 18px; }
  .activity-row-status { font-size: 12px; line-height: 16px; }
  .activity-row-status-desktop { display: none; }
  .activity-row-status-touch { display: block; }
  .activity-row-read { align-self: center; flex: 0 0 44px; width: 44px; min-height: 44px; height: 44px; padding: 3px 0; opacity: 1; gap: 0; }
  .activity-read-all svg, .activity-row-read svg { width: 15px; height: 15px; }
  .activity-empty { margin-left: 7px; font-size: 12px; }
}
@media (max-width: 360px) {
  .activity-folder-icon { display: none; }
}
@media (hover: none) {
  .activity-row:not(.is-selected):hover, .activity-folder-toggle:hover, .activity-read-all:hover { background: transparent; }
  .activity-row-open:active, .activity-read-all:active, .activity-row-read:active { background: rgba(161,161,170,.15); }
}
:global(html.dark) .activity-folder { color: #d4d4d8; border-color: rgba(161,161,170,.2); }
:global(html.dark) .activity-row:hover, :global(html.dark) .activity-folder-toggle:hover, :global(html.dark) .activity-read-all:hover { background: rgba(63,63,70,.58); }
:global(html.dark) .activity-row.is-selected { background: rgba(59,130,246,.14); color: #bfdbfe; }
:global(html.dark) .activity-row-status { color: #a1a1aa; }
:global(html.dark) .activity-row-read { color: #a1a1aa !important; }
:global(html.dark) .activity-waiting-count { background: rgba(217,153,57,.16); color: #e5b36e; }
:global(html.dark) .activity-row.is-waiting .activity-row-status { color: #e5b36e; }
@media (hover: none) {
  :global(html.dark) .activity-row:not(.is-selected):hover, :global(html.dark) .activity-folder-toggle:hover, :global(html.dark) .activity-read-all:hover { background: transparent; }
}
</style>
