<script setup>
import { computed, nextTick, ref, useId } from 'vue';
import FloatingPanel from '../references/FloatingPanel.vue';
import { flag, noAutofill, regionName } from '../servers/serverHelpers.js';
import ServerForm from './ServerForm.vue';
import { serverChannel, serverLabel, serverUnavailable, serverUse } from './instanceHelpers.js';

// The "添加服务器…" entry opens the server dialog on top of the current one,
// so the surrounding form keeps what was already filled in.
const props = defineProps({modelValue: {type: String, default: ''}, servers: {type: Array, required: true}, label: {type: String, default: '运行位置'}});
const emit = defineEmits(['update:modelValue', 'added']);
const adding = ref(false), formRef = ref(null), button = ref(null), search = ref(null), floating = ref(null);
const open = ref(false), query = ref(''), active = ref(0);
const listId = `ob-server-${useId()}`;

const current = computed(() => props.servers.find(s => s.id === props.modelValue));
const inUse = server => server.local || serverUse(server).instances.length > 0 || serverUse(server).gateway;
const serverRow = server => ({kind: 'server', key: server.id, server, disabled: Boolean(serverUnavailable(server))});
// The host itself and servers already running something first; a search is one flat list.
const rows = computed(() => {
  const q = query.value.trim().toLowerCase();
  const out = [];
  if (q) {
    out.push(...props.servers.filter(s => [s.name, s.host, s.region, regionName(s.region)].some(v => String(v || '').toLowerCase().includes(q))).map(serverRow));
  } else {
    const used = props.servers.filter(inUse), others = props.servers.filter(s => !inUse(s));
    if (used.length) out.push({kind: 'caption', key: '_used', name: '已在使用'}, ...used.map(serverRow));
    if (others.length) out.push({kind: 'caption', key: '_others', name: '其他服务器'}, ...others.map(serverRow));
  }
  out.push({kind: 'add', key: '_add'});
  return out;
});
const choices = computed(() => rows.value.filter(r => r.kind !== 'caption' && !r.disabled));
const indexOf = row => choices.value.indexOf(row);
const useText = server => {
  const {gateway, instances} = serverUse(server);
  return [instances.length ? `${instances.length} 个实例` : '', gateway ? '网关' : ''].filter(Boolean);
};
const useTitle = server => serverUse(server).instances.join('、');

async function toggle() {
  if (open.value) { close(); return; }
  query.value = '';
  open.value = true;
  active.value = Math.max(0, choices.value.findIndex(r => r.server?.id === props.modelValue));
  // The panel stays hidden until positioned, and hidden inputs cannot take focus.
  await nextTick();
  await floating.value?.position();
  await nextTick();
  if (!open.value) return;
  // On touch screens a focused search raises the keyboard over the list.
  if (!window.matchMedia?.('(pointer: coarse)').matches) search.value?.focus({preventScroll: true});
  scrollActive();
}
function close(refocus = false) { open.value = false; if (refocus) button.value?.focus(); }
function keepFocus(event) { if (event.pointerType === 'mouse') event.preventDefault(); }
function pick(row) {
  if (!row || row.disabled) return;
  close(true);
  if (row.kind === 'add') adding.value = true;
  else emit('update:modelValue', row.server.id);
}
function scrollActive() { nextTick(() => document.getElementById(`${listId}-${active.value}`)?.scrollIntoView({block: 'nearest'})); }
function typed() { active.value = 0; scrollActive(); }
function key(event) {
  if (event.isComposing || event.keyCode === 229) return;
  const n = choices.value.length;
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    if (n) { active.value = (active.value + (event.key === 'ArrowDown' ? 1 : -1) + n) % n; scrollActive(); }
  } else if (event.key === 'Enter') {
    event.preventDefault();
    pick(choices.value[active.value]);
  } else if (event.key === 'Escape') {
    // Only the picker closes, not the dialog around it.
    event.preventDefault();
    event.stopPropagation();
    close(true);
  }
}
function saved(server) {
  adding.value = false;
  emit('added', server);
  emit('update:modelValue', server.id);
}
function closeDialog(done) { if (!formRef.value?.busy) done(); }
</script>
<template>
  <button ref="button" type="button" class="ss-trigger" :aria-label="`${label}：${serverLabel(current) || '未选择'}`"
    aria-haspopup="listbox" :aria-expanded="open" @click="toggle">
    <template v-if="current">
      <span class="ss-flag" aria-hidden="true">{{ flag(current.region) || '🖥' }}</span>
      <span class="ss-name">{{ current.name }}</span>
      <span v-if="current.local" class="ss-tag is-host">主控</span>
      <span v-for="text in useText(current)" :key="text" class="ss-tag">{{ text }}</span>
      <span v-if="serverChannel(current)" class="ss-via">{{ serverChannel(current) }}</span>
    </template>
    <span v-else class="ss-name is-empty">选择服务器</span>
  </button>
  <FloatingPanel ref="floating" :open="open" :anchor="button" placement="bottom-start" :width="380" :label="`选择${label}`" @close="close()">
    <div class="ss-panel">
      <input ref="search" v-model="query" v-bind="noAutofill" type="search" class="ss-search" placeholder="搜索名称、地区或地址"
        aria-label="搜索服务器" role="combobox" aria-autocomplete="list" :aria-controls="listId"
        :aria-activedescendant="choices[active] ? `${listId}-${active}` : undefined" @input="typed" @keydown="key" />
      <ul :id="listId" class="ss-list" role="listbox" :aria-label="label">
        <template v-for="row in rows" :key="row.key">
          <li v-if="row.kind === 'caption'" class="ss-caption" role="presentation">{{ row.name }}</li>
          <li v-else-if="row.kind === 'add'" :id="`${listId}-${indexOf(row)}`" role="option" class="ss-option is-add" :class="{'is-active': indexOf(row) === active}"
            aria-selected="false" @pointerdown="keepFocus" @click="pick(row)" @pointerenter="active = indexOf(row)">
            <span class="ss-flag" aria-hidden="true">＋</span><span class="ss-name">添加服务器…</span>
          </li>
          <li v-else :id="row.disabled ? undefined : `${listId}-${indexOf(row)}`" role="option" class="ss-option"
            :class="{'is-active': !row.disabled && indexOf(row) === active}" :aria-selected="row.server.id === modelValue" :aria-disabled="row.disabled || undefined"
            :title="row.server.local ? '' : regionName(row.server.region)" @pointerdown="keepFocus" @click="pick(row)" @pointerenter="row.disabled || (active = indexOf(row))">
            <span class="ss-flag" aria-hidden="true">{{ flag(row.server.region) || '🖥' }}</span>
            <span class="ss-name">{{ row.server.name }}</span>
            <span class="ss-marks">
              <span v-if="row.server.local" class="ss-tag is-host">主控</span>
              <span v-for="text in useText(row.server)" :key="text" class="ss-tag" :title="useTitle(row.server)">{{ text }}</span>
              <span class="ss-via">{{ serverUnavailable(row.server) || serverChannel(row.server) }}</span>
            </span>
          </li>
        </template>
        <li v-if="query && rows.length === 1" class="ss-empty" role="presentation">没有匹配的服务器</li>
      </ul>
    </div>
  </FloatingPanel>
  <el-dialog v-model="adding" title="添加服务器" width="500px" class="admin-dialog instance-dialog instance-ui" append-to-body destroy-on-close :close-on-click-modal="false" :before-close="closeDialog">
    <ServerForm v-if="adding" ref="formRef" @saved="saved" @cancel="adding = false" />
  </el-dialog>
</template>
