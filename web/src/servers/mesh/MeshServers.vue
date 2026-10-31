<script setup>
import { computed, ref } from 'vue';
import { flag, noAutofill } from '../serverHelpers.js';
import { mesh } from './meshStore.js';
import { isRelay, nodeState } from './meshHelpers.js';
import { DRAG_TYPE } from './meshGraph.js';

// Servers in the mesh and servers that can join. Desktop: drag a server onto the
// board (Enter adds it in the next free place). Phone: tap to add.
const props = defineProps({items: {type: Array, default: () => []}, phone: Boolean, installing: {type: Object, default: () => ({})}, selected: {type: String, default: ''}});
const emit = defineEmits(['select', 'add']);
const query = ref('');
const regionOf = id => props.items.find(i => i.id === id)?.region;
const match = name => !query.value.trim() || String(name || '').toLowerCase().includes(query.value.trim().toLowerCase());
const joined = computed(() => (mesh.work?.members || []).filter(m => match(m.name)));
const busy = id => Boolean(props.installing[id]);
const joinable = computed(() => (mesh.data?.eligible || []).filter(e => !(mesh.work?.members || []).some(m => m.serverId === e.serverId) && match(e.name))
  .sort((a, b) => Number(b.ok) - Number(a.ok)));
function dragStart(event, row) {
  if (!row.ok || busy(row.serverId)) { event.preventDefault(); return; }
  event.dataTransfer.setData(DRAG_TYPE, row.serverId);
  event.dataTransfer.effectAllowed = 'copy';
}
</script>
<template>
  <div class="mesh-servers" :class="{'is-phone': phone}">
    <!-- The search stays on top; only the lists under it scroll. -->
    <div class="mesh-servers-top">
      <input v-model="query" v-bind="noAutofill" type="search" class="mesh-servers-search" placeholder="搜索服务器" aria-label="搜索服务器" />
      <slot name="aside" />
    </div>
    <div class="mesh-servers-scroll">
      <section class="mesh-servers-group" aria-label="可加入">
        <h3>可加入<span class="instance-muted">{{ phone ? '点一下加入' : '拖到画板上加入' }}</span></h3>
        <ul v-if="joinable.length">
          <li v-for="row in joinable" :key="row.serverId">
            <button type="button" class="mesh-server" :class="{'is-disabled': !row.ok}" :disabled="!row.ok || busy(row.serverId)" :draggable="!phone && row.ok && !busy(row.serverId)"
              :aria-label="row.ok ? `${row.name}，${phone ? '加入组网' : '拖到画板上加入组网'}` : `${row.name}：${row.reason || '不能加入'}`"
              @dragstart="dragStart($event, row)" @click="phone && emit('add', row.serverId)" @keydown.enter.prevent="!phone && emit('add', row.serverId)">
              <span class="sv-flag" aria-hidden="true">{{ flag(regionOf(row.serverId)) }}</span>
              <span class="mesh-server-name">{{ row.name }}</span>
              <small v-if="busy(row.serverId)" class="mesh-server-note" data-tone="warn">安装中…</small>
              <small v-else-if="!row.ok" class="mesh-server-note">{{ row.reason || '不能加入' }}</small>
              <span v-else-if="phone" class="mesh-server-add" aria-hidden="true">＋</span>
            </button>
          </li>
        </ul>
        <p v-else class="instance-muted">{{ query ? '没有匹配的服务器' : '没有可以加入的服务器。先在“添加服务器”里装好节点。' }}</p>
      </section>
      <section class="mesh-servers-group" aria-label="网格中">
        <h3>网格中<span class="instance-muted">{{ (mesh.work?.members || []).length }}</span></h3>
        <ul v-if="joined.length">
          <li v-for="m in joined" :key="m.serverId">
            <button type="button" class="mesh-server" :aria-pressed="selected === m.serverId" :aria-label="`选中 ${m.name}`" @click="emit('select', m.serverId)">
              <span class="sv-flag" aria-hidden="true">{{ flag(regionOf(m.serverId)) }}</span>
              <span class="mesh-server-name" :class="{'is-removing': m.removing}">{{ m.name }}</span>
              <span v-if="isRelay(m)" class="pb-tag is-plain">中转</span>
              <span class="pb-dot" :data-tone="nodeState(mesh.data, m.serverId)[0]" :title="nodeState(mesh.data, m.serverId)[1]" aria-hidden="true"></span>
            </button>
          </li>
        </ul>
        <p v-else class="instance-muted">{{ query ? '没有匹配的成员' : '还没有成员' }}</p>
      </section>
    </div>
  </div>
</template>
