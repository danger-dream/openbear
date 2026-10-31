<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { ChevronRight } from '@lucide/vue';
import { DdnsApi } from './ddnsApi.js';
import { currentValue, recordUsesServer, stateTone } from './ddnsHelpers.js';
import './ddns.css';

// The 动态域名 card on a server's detail page: the records this machine is the source of.
// Hidden while the controller has no DDNS (older version, or the read failed). panel: a
// section of the mesh member's status page instead of a card.
const props = defineProps({serverId: {type: String, required: true}, active: Boolean, panel: Boolean});
const data = ref(null);
let timer, alive = true;
const list = computed(() => (data.value?.records || []).filter(r => recordUsesServer(r, props.serverId)));
const backup = r => r.source?.serverId !== props.serverId;
const isNode = computed(() => (data.value?.nodes || []).some(n => n.serverId === props.serverId));

async function load() {
  try { const next = await DdnsApi.list(); if (alive) data.value = next; }
  catch { /* keeps the last reading; none means the card stays hidden */ }
}
function poll() {
  clearTimeout(timer);
  if (props.active && !document.hidden) void load();
  timer = setTimeout(poll, 30000);
}
// The page decides how to get there (App listens), so the card works wherever it is shown.
function open(params) { window.dispatchEvent(new CustomEvent('openbear:open-ddns', {detail: {serverId: props.serverId, ...params}})); }
watch(() => props.active, value => { if (value) poll(); });
onMounted(poll);
onBeforeUnmount(() => { alive = false; clearTimeout(timer); });
</script>
<template>
  <section v-if="data && (list.length || isNode)" :class="panel ? 'dd-card dd-card-panel' : 'ix-card dd-card'" aria-label="动态域名">
    <header :class="panel ? 'dd-panel-head' : 'ix-card-head'"><component :is="panel ? 'h4' : 'h3'">动态域名</component><span class="ob-grow" />
      <button v-if="list.length" type="button" class="ix-text-button" @click="open({})">全部查看</button>
      <button v-else type="button" class="ix-text-button" @click="open({add: true})">添加解析</button>
    </header>
    <ul v-if="list.length" class="dd-card-list">
      <li v-for="r in list" :key="r.id">
        <button type="button" class="dd-card-row" @click="open({recordId: r.id})">
          <span class="dd-card-name ix-ellipsis">{{ r.name }}</span>
          <span class="dd-type">{{ r.type }}</span>
          <span class="dd-card-value mono ix-ellipsis">{{ backup(r) ? '备用 · ' : '' }}{{ currentValue(r) }}</span>
          <span class="ix-pill" :data-tone="stateTone(r.state)">{{ r.stateText || r.state }}</span>
          <ChevronRight :size="14" class="dd-card-go" aria-hidden="true" />
        </button>
      </li>
    </ul>
    <p v-else class="instance-muted dd-card-empty">没有以这台机器为来源的解析</p>
  </section>
</template>
