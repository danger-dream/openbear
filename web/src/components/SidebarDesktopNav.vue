<script setup>
import { computed, ref } from 'vue';
import FloatingPanel from '../references/FloatingPanel.vue';

// Up to six destinations fit in one row; beyond that the owner keeps five
// pinned and the rest wait under "More".
const PINNED_STORAGE = 'openbear.sidebar.pinned';
const LIMIT = 5;
const DEFAULT_PINNED = ['memory', 'secrets', 'docs', 'instances', 'servers'];

const props = defineProps({items: {type: Array, default: () => []}, active: {type: String, default: ''}});
const emit = defineEmits(['select', 'shelf-enter', 'shelf-leave', 'shelf-key']);
const saved = ref(readPinned());
const open = ref(false), moreButton = ref(null);

function readPinned() {
  try {
    const value = JSON.parse(localStorage.getItem(PINNED_STORAGE) || 'null');
    return Array.isArray(value) ? value.filter(key => typeof key === 'string') : null;
  } catch { return null; }
}
const overflow = computed(() => props.items.length > LIMIT + 1);
const pinnedKeys = computed(() => {
  const keys = props.items.map(item => item.key);
  const chosen = (saved.value || DEFAULT_PINNED).filter(key => keys.includes(key)).slice(0, LIMIT);
  return chosen.length ? chosen : keys.slice(0, LIMIT);
});
// Pinned entries keep the navigation order, whatever order they were pinned in.
const shown = computed(() => overflow.value ? props.items.filter(item => pinnedKeys.value.includes(item.key)) : props.items);
const rest = computed(() => overflow.value ? props.items.filter(item => !pinnedKeys.value.includes(item.key)) : []);
const moreActive = computed(() => rest.value.some(item => item.key === props.active));
const full = computed(() => pinnedKeys.value.length >= LIMIT);

function store(keys) {
  saved.value = keys;
  try { localStorage.setItem(PINNED_STORAGE, JSON.stringify(keys)); } catch { /* private mode keeps it for this page only */ }
}
function pin(key) { if (!full.value) store([...pinnedKeys.value, key]); }
function unpin(key) { if (pinnedKeys.value.length > 1) store(pinnedKeys.value.filter(k => k !== key)); }
function go(key) { open.value = false; emit('select', key); }
const pinnedItems = computed(() => props.items.filter(item => pinnedKeys.value.includes(item.key)));
const groups = computed(() => [['未固定', rest.value], [full.value ? `已固定 ${LIMIT}/${LIMIT} · 已满，先取消一个` : `已固定 ${pinnedKeys.value.length}/${LIMIT}`, pinnedItems.value]]);
</script>
<template>
  <nav class="sidebar-desktop-nav sidebar-resource-grid text-sm" :style="{'--sidebar-resource-columns': shown.length + (overflow ? 1 : 0)}" aria-label="资源与设置">
    <button
      v-for="n in shown"
      :key="n.key"
      type="button"
      :aria-current="active === n.key ? 'page' : undefined"
      @click="emit('select', n.key)"
      @pointerenter="emit('shelf-enter', $event, n.key)"
      @pointerleave="emit('shelf-leave')"
      @keydown="emit('shelf-key', $event, n.key)"
      class="sidebar-resource-tile"
    >
      <el-icon class="sidebar-resource-icon" :size="16" aria-hidden="true"><component :is="n.icon" /></el-icon>
      <span class="sidebar-resource-label">{{ n.shortLabel || n.label }}</span>
    </button>
    <button v-if="overflow" ref="moreButton" type="button" class="sidebar-resource-tile" :aria-current="moreActive ? 'page' : undefined"
      aria-haspopup="dialog" :aria-expanded="open" aria-label="更多入口" @click="open = !open">
      <el-icon class="sidebar-resource-icon" :size="16" aria-hidden="true"><MoreFilled /></el-icon>
      <span class="sidebar-resource-label">更多</span>
    </button>
  </nav>
  <FloatingPanel :open="open" :anchor="moreButton" placement="bottom-end" :width="232" label="更多入口" @close="open = false">
    <div class="sidebar-more">
      <template v-for="[title, list] in groups" :key="title">
        <p v-if="list.length" class="sidebar-more-caption">{{ title }}</p>
        <div v-for="n in list" :key="n.key" class="sidebar-more-row" :class="{'is-active': active === n.key}">
          <button type="button" class="sidebar-more-go" :aria-current="active === n.key ? 'page' : undefined" @click="go(n.key)">
            <el-icon :size="15" aria-hidden="true"><component :is="n.icon" /></el-icon><span>{{ n.label }}</span>
          </button>
          <button type="button" class="sidebar-more-pin" :aria-pressed="pinnedKeys.includes(n.key)"
            :disabled="pinnedKeys.includes(n.key) ? pinnedKeys.length <= 1 : full"
            :aria-label="`${pinnedKeys.includes(n.key) ? '取消固定' : '固定'} ${n.label}`"
            :title="pinnedKeys.includes(n.key) ? (pinnedKeys.length <= 1 ? '至少保留一个常用入口' : '从侧边栏取消固定') : full ? '常用最多 5 个，先取消一个' : '固定到侧边栏'"
            @click="pinnedKeys.includes(n.key) ? unpin(n.key) : pin(n.key)">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 17v5" /><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z" /></svg>
          </button>
        </div>
      </template>
    </div>
  </FloatingPanel>
</template>
