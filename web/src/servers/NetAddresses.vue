<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { NetApi } from './netApi.js';
import { addressList, firstAddresses, IFACE_KINDS } from './netHelpers.js';
import { safeError } from './serverHelpers.js';
import './net.css';

// The addresses on a server's interfaces, grouped by interface; a tap copies one.
// card: the detail page's card; otherwise a section of the mesh member's status page,
// which shows the first `limit` addresses and the rest behind a tap.
const props = defineProps({serverId: {type: String, required: true}, online: {type: Boolean, default: true}, card: Boolean, title: {type: String, default: '网络地址'},
  limit: {type: Number, default: 0}});
const reply = ref(null), state = ref('idle'), error = ref(''), open = ref(false);
let round = 0;

const list = computed(() => addressList(reply.value));
const shown = computed(() => open.value ? list.value.interfaces : firstAddresses(list.value.interfaces, props.limit));
const hidden = computed(() => props.limit ? Math.max(0, list.value.count - props.limit) : 0);
// Offline or too old a node: said in place of the list.
const blocked = computed(() => !props.online ? '节点离线' : state.value === 'upgrade' ? '节点版本过旧，升级节点后可用' : state.value === 'offline' ? '节点离线' : '');

async function load() {
  const mine = ++round;
  if (!props.online) { state.value = 'idle'; return; }
  state.value = reply.value ? 'refreshing' : 'loading';
  try {
    const data = await NetApi.addresses(props.serverId);
    if (mine !== round) return;
    reply.value = data; state.value = 'ok'; error.value = '';
  } catch (e) {
    if (mine !== round) return;
    const code = e?.response?.data?.error;
    if (code === 'node_upgrade_required') { state.value = 'upgrade'; reply.value = null; }
    else if (code === 'node_offline') { state.value = 'offline'; reply.value = null; }
    else { error.value = safeError(e); state.value = reply.value ? 'ok' : 'error'; }
  }
}
async function copy(ip) {
  try { await navigator.clipboard.writeText(ip); ElMessage.success(`已复制 ${ip}`); }
  catch { ElMessage.warning('浏览器不允许复制，请手动选择文字'); }
}
// Loaded once when shown, again when the server changes or the node comes back online.
watch(() => [props.serverId, props.online], ([, online], old) => {
  if (old && old[0] !== props.serverId) { reply.value = null; open.value = false; error.value = ''; }
  if (online) void load(); else round++;
}, {immediate: true});
onBeforeUnmount(() => { round++; });
</script>
<template>
  <section :class="card ? 'ix-card net-addresses' : 'net-addresses net-addresses-panel'" :aria-label="title">
    <header :class="card ? 'ix-card-head' : 'net-panel-head'">
      <component :is="card ? 'h3' : 'h4'">{{ title }}</component>
      <span class="ob-grow" />
      <button v-if="!blocked" type="button" class="ix-text-button net-refresh" :disabled="state === 'loading' || state === 'refreshing'" @click="load">{{ state === 'refreshing' ? '正在刷新…' : '刷新' }}</button>
    </header>
    <p v-if="blocked" class="instance-muted net-state">{{ blocked }}</p>
    <p v-else-if="state === 'loading' || state === 'idle'" class="instance-muted net-state" role="status">正在读取…</p>
    <p v-else-if="state === 'error'" class="instance-muted net-state">读不到地址：{{ error }}</p>
    <template v-else>
      <p v-if="!list.count" class="instance-muted net-state">没有可列出的地址。</p>
      <div v-for="i in shown" :key="i.name" class="net-iface" :data-down="!i.up || undefined">
        <div class="net-iface-head">
          <span class="mono net-iface-name">{{ i.name }}</span>
          <span v-if="IFACE_KINDS[i.kind]" class="pb-tag" :data-tone="i.kind === 'mesh' ? 'info' : undefined">{{ IFACE_KINDS[i.kind] }}</span>
          <span v-if="!i.up" class="pb-tag" data-tone="warn">未启用</span>
        </div>
        <ul class="net-addr-list">
          <li v-for="a in i.addrs" :key="a.text">
            <button type="button" class="net-addr" :data-control="a.control || undefined" :title="a.control ? `复制 ${a.ip}（主控管理这台机器用的地址，平时用不到）` : `复制 ${a.ip}`" @click="copy(a.ip)">
              <span class="mono net-addr-ip">{{ a.text }}</span>
              <span v-for="[tone, label] in a.tags" :key="label" class="pb-tag" :data-tone="tone || undefined">{{ label }}</span>
            </button>
          </li>
        </ul>
      </div>
      <p v-for="ip in list.exits" :key="ip" class="net-exit">
        <button type="button" class="net-addr" :title="`复制 ${ip}`" @click="copy(ip)"><span class="instance-muted">公网出口</span><span class="mono net-addr-ip">{{ ip }}</span></button>
        <small class="instance-muted">{{ ip === list.line ? '国内服务看到的地址，不在本机网卡上' : '不在本机网卡上，经 NAT 或代理出去' }}</small>
      </p>
      <p v-if="list.proxy" class="net-exit" data-proxy>
        <button type="button" class="net-addr" :title="`复制 ${list.proxy}`" @click="copy(list.proxy)"><span class="instance-muted">出国出口</span><span class="mono net-addr-ip">{{ list.proxy }}</span><span class="pb-tag" data-tone="warn">经代理</span></button>
        <small class="instance-muted">国外服务看到的地址：去国外的流量经过代理，从这里出去</small>
      </p>
      <button v-if="hidden" type="button" class="ix-text-button mesh-fold-toggle net-more" :aria-expanded="open" @click="open = !open">{{ open ? '收起' : `展开其余 ${hidden} 个地址` }}</button>
      <p v-if="error" class="instance-muted net-state">暂时读不到最新地址（{{ error }}）。</p>
    </template>
  </section>
</template>
