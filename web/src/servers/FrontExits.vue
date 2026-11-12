<script setup>
import { computed, reactive, watch } from 'vue';
import { NetApi } from './netApi.js';
import { SCOPES } from './netHelpers.js';

// A front's (前置机) exits: the primary and an optional backup, each with the address of the exit the front reaches it at.
const props = defineProps({modelValue: {type: Array, required: true}, servers: {type: Array, default: () => []}, self: {type: String, default: ''}});
const emit = defineEmits(['update:modelValue']);
// Exits are other servers with a node, not the controller's machine and not fronts themselves.
const choices = computed(() => props.servers.filter(s => !s.local && s.id !== props.self && !s.profile?.front && s.node));
function set(i, patch) {
  emit('update:modelValue', props.modelValue.map((e, j) => j === i ? {...e, ...patch} : {...e}));
}
function pickExit(i, serverId) { set(i, {serverId, address: ''}); void load(serverId, true); }
function addBackup() { emit('update:modelValue', [...props.modelValue, {serverId: '', address: ''}]); }
// Either one goes when there are two; the one left is the primary.
function remove(i) { emit('update:modelValue', props.modelValue.filter((_, j) => j !== i)); }
const label = s => `${s.name}${s.live ? '' : '（离线）'}`;

// The addresses on each exit's interfaces, then a public one behind NAT; mesh and link-local ones cannot be the line.
const known = reactive({});
async function load(id, again = false) {
  if (!id || known[id]?.state === 'loading' || known[id] && !again || known[id]?.state === 'ok') return;
  known[id] = {state: 'loading', options: []};
  try {
    const reply = await NetApi.addresses(id), seen = new Set(), options = [];
    for (const iface of Array.isArray(reply?.interfaces) ? reply.interfaces : []) {
      for (const a of Array.isArray(iface?.addrs) ? iface.addrs : []) {
        if (!a?.ip || a.scope === 'mesh' || a.scope === 'linklocal' || seen.has(a.ip)) continue;
        seen.add(a.ip);
        options.push({ip: String(a.ip), note: [iface.name, a.egress ? '公网出口' : SCOPES[a.scope]?.[1]].filter(Boolean).join(' · ')});
      }
    }
    for (const ip of [reply?.publicIPv4, reply?.publicIPv6]) if (ip && !seen.has(ip)) { seen.add(ip); options.push({ip: String(ip), note: '公网出口'}); }
    options.sort((x, y) => Number(x.ip.includes(':')) - Number(y.ip.includes(':')));
    known[id] = {state: 'ok', options};
  } catch (e) {
    known[id] = {state: e?.response?.data?.error === 'node_offline' ? 'offline' : 'error', options: []};
  }
}
watch(() => props.modelValue.map(e => e.serverId), ids => ids.forEach(id => void load(id)), {immediate: true});
// A saved address the exit no longer reports stays selectable.
function options(e) {
  const list = known[e.serverId]?.options || [];
  return e.address && !list.some(o => o.ip === e.address) ? [{ip: e.address, note: '当前'}, ...list] : list;
}
function prompt(e) {
  if (!e.serverId) return '先选出口';
  const state = known[e.serverId]?.state;
  if (state === 'loading') return '正在读取地址…';
  if (state === 'offline') return '出口离线，读不到地址';
  if (state === 'error') return '读不到出口的地址';
  return known[e.serverId]?.options.length ? '选择地址' : '出口没有可用地址';
}
</script>
<template>
  <div class="ob-ctl sv-front-exits">
    <div v-for="(e, i) in modelValue" :key="i" class="sv-front-exit" :class="{'has-remove': modelValue.length > 1}">
      <select :value="e.serverId" required :aria-label="i ? '备用出口' : '出口'" @change="pickExit(i, $event.target.value)">
        <option value="" disabled>{{ i ? '选择备用出口' : '选择出口' }}</option>
        <option v-for="s in choices" :key="s.id" :value="s.id" :disabled="modelValue.some((x, j) => j !== i && x.serverId === s.id)">{{ label(s) }}</option>
      </select>
      <select :value="e.address" required :disabled="!e.serverId" :aria-label="i ? '备用出口地址' : '出口地址'" @change="set(i, {address: $event.target.value})">
        <option value="" disabled>{{ prompt(e) }}</option>
        <option v-for="o in options(e)" :key="o.ip" :value="o.ip">{{ o.note ? `${o.ip}（${o.note}）` : o.ip }}</option>
      </select>
      <button v-if="modelValue.length > 1" type="button" class="ix-text-button" :aria-label="i ? '去掉备用出口' : '去掉这个出口'" @click="remove(i)">去掉</button>
    </div>
    <button v-if="modelValue.length < 2" type="button" class="ix-text-button sv-front-add" @click="addBackup">加一个备用出口</button>
    <small>出口是和前置机组成专线的服务器。地址选前置机连它用的那个，比如专线内网地址。前置机只经这些出口连 OpenBear，不会自己连别的地方。</small>
  </div>
</template>
