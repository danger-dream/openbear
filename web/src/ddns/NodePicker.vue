<script setup>
import { computed, nextTick, ref, useId } from 'vue';
import FloatingPanel from '../references/FloatingPanel.vue';
import { flag, noAutofill, regionName } from '../servers/serverHelpers.js';

// Flag, name and current IPv4 of each node (offline ones say so); searchable, and driven by the keyboard
// from the search box: ↑ ↓ move, Enter picks, Esc closes only the list, not the dialog around it.
// outdated: whether a node is marked 需升级 (by default, one whose node cannot do dynamic DNS).
const props = defineProps({modelValue: {type: String, default: ''}, nodes: {type: Array, required: true}, label: {type: String, default: '节点'},
  none: {type: String, default: ''}, exclude: {type: String, default: ''}, outdated: {type: Function, default: node => !node.ddns}});
const emit = defineEmits(['update:modelValue']);
const button = ref(null), search = ref(null), floating = ref(null);
const open = ref(false), query = ref(''), active = ref(0);
const listId = `dd-node-${useId()}`;

const current = computed(() => props.nodes.find(n => n.serverId === props.modelValue) || null);
const note = node => !node.online ? '离线' : node.v4 || node.v6 || '没有公网地址';
const rows = computed(() => {
  const q = query.value.trim().toLowerCase();
  const nodes = props.nodes.filter(n => n.serverId !== props.exclude && (!q || [n.name, n.v4, n.v6, regionName(n.region)].some(v => String(v || '').toLowerCase().includes(q))
    || String(n.region || '').toLowerCase() === q));
  return [...(props.none && !q ? [{serverId: '', none: true}] : []), ...nodes];
});

async function toggle() {
  if (open.value) { close(); return; }
  query.value = '';
  open.value = true;
  active.value = Math.max(0, rows.value.findIndex(r => r.serverId === props.modelValue));
  await nextTick();
  await floating.value?.position();
  await nextTick();
  if (!open.value) return;
  // A focused search on a touch screen raises the keyboard over the list.
  if (!window.matchMedia?.('(pointer: coarse)').matches) search.value?.focus({preventScroll: true});
  scrollActive();
}
function close(refocus = false) { open.value = false; if (refocus) button.value?.focus(); }
function pick(row) {
  if (!row) return;
  close(true);
  if (row.serverId !== props.modelValue) emit('update:modelValue', row.serverId);
}
// The list is teleported out of any dialog around the picker; a dialog's focus trap would pull focus
// back from the search box, so a move into the list is not reported to it.
function leaving(event) { if (event.relatedTarget?.closest?.('.dd-node-panel')) event.stopPropagation(); }
function scrollActive() { nextTick(() => document.getElementById(`${listId}-${active.value}`)?.scrollIntoView({block: 'nearest'})); }
function typed() { active.value = 0; scrollActive(); }
function key(event) {
  if (event.isComposing || event.keyCode === 229) return;
  const n = rows.value.length;
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    if (n) { active.value = (active.value + (event.key === 'ArrowDown' ? 1 : -1) + n) % n; scrollActive(); }
  } else if (event.key === 'Enter') {
    event.preventDefault();
    pick(rows.value[active.value]);
  } else if (event.key === 'Escape') {
    event.preventDefault();
    event.stopPropagation();
    close(true);
  }
}
</script>
<template>
  <button ref="button" type="button" class="ss-trigger dd-node-trigger" :aria-label="`${label}：${current?.name || none || '未选择'}`"
    aria-haspopup="listbox" :aria-expanded="open" @focusout="leaving" @click="toggle" @keydown.down.prevent="open || toggle()">
    <template v-if="current">
      <span class="ss-flag" aria-hidden="true">{{ flag(current.region) || '🖥' }}</span>
      <span class="ss-name">{{ current.name }}</span>
      <span v-if="outdated(current)" class="ss-tag dd-tag-warn">需升级</span>
      <span class="ss-via mono" :class="{'dd-off': !current.online}">{{ note(current) }}</span>
    </template>
    <span v-else class="ss-name is-empty">{{ none || '选择节点' }}</span>
  </button>
  <FloatingPanel ref="floating" :open="open" :anchor="button" placement="bottom-start" :width="400" :label="`选择${label}`" @close="close()">
    <div class="ss-panel dd-node-panel">
      <input ref="search" v-model="query" v-bind="noAutofill" type="search" class="ss-search" placeholder="搜索节点名称、国家代码或地址"
        aria-label="搜索节点" role="combobox" aria-autocomplete="list" :aria-expanded="true" :aria-controls="listId"
        :aria-activedescendant="rows[active] ? `${listId}-${active}` : undefined" @input="typed" @keydown="key" />
      <ul :id="listId" class="ss-list" role="listbox" :aria-label="label">
        <li v-for="(row, i) in rows" :id="`${listId}-${i}`" :key="row.serverId || '_none'" role="option" class="ss-option"
          :class="{'is-active': i === active, 'is-off': !row.none && !row.online}" :aria-selected="row.serverId === modelValue"
          :title="row.none ? '' : regionName(row.region)" @pointerdown.prevent="pick(row)" @pointerenter="active = i">
          <template v-if="row.none"><span class="ss-flag" aria-hidden="true"></span><span class="ss-name">{{ none }}</span></template>
          <template v-else>
            <span class="ss-flag" aria-hidden="true">{{ flag(row.region) || '🖥' }}</span>
            <span class="ss-name">{{ row.name }}</span>
            <span class="ss-marks">
              <span v-if="outdated(row)" class="ss-tag dd-tag-warn">需升级</span>
              <span class="ss-via mono">{{ note(row) }}</span>
            </span>
          </template>
        </li>
        <li v-if="!rows.length" class="ss-empty" role="presentation">{{ nodes.length ? '没有匹配的节点' : '还没有装了节点的服务器' }}</li>
      </ul>
    </div>
  </FloatingPanel>
</template>
