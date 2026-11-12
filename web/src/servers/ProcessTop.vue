<script setup>
import { computed, onActivated, onBeforeUnmount, onDeactivated, onMounted, ref } from 'vue';
import { Api } from '../api.js';
import { bytes, rate, safeError } from './serverHelpers.js';

// The busiest processes by one figure, asked of the node while this is open and every 3 seconds; not while the
// browser tab is in the background or the page kept alive behind another one, and once more on coming back.
// Ending one is confirmed in its row: a dialog would close the popover around it.
const props = defineProps({server: {type: Object, required: true}, sort: {type: String, default: 'cpu'}});
const SORTS = [['cpu', 'CPU'], ['mem', '内存'], ['io', '读写'], ['conn', '连接'], ['net', '网络']];
const TITLES = {cpu: 'CPU 占用最高的进程', mem: '内存占用最高的进程', io: '读写最多的进程', conn: '连接最多的进程', net: '网络流量去向'};
// Linux keeps per-process I/O and sockets; macOS and Windows list CPU and memory.
const LINUX_ONLY = new Set(['io', 'conn', 'net']);
const linux = computed(() => (props.server.node?.info?.platform || 'linux') === 'linux');
const current = ref(linux.value || !LINUX_ONLY.has(props.sort) ? props.sort : 'cpu');
const data = ref(null), error = ref(''), stale = ref(false);
const confirming = ref(0), ending = ref(0), stuck = ref(0), note = ref('');
const sorts = computed(() => SORTS.filter(([key]) => linux.value || !LINUX_ONLY.has(key)));
const canEnd = computed(() => props.server.node?.caps?.exec !== false);
const rows = computed(() => data.value?.sort === current.value ? data.value.processes || [] : []);
const busyNics = computed(() => (data.value?.interfaces || []).filter(n => n.rxRate || n.txRate));
const quietNics = computed(() => (data.value?.interfaces || []).length - busyNics.value.length);
const busyNetns = computed(() => (data.value?.netns || []).filter(n => n.rxRate || n.txRate));
let timer = 0, generation = 0, alive = true, away = false;

async function load() {
  const mine = ++generation;
  clearTimeout(timer);
  try {
    const result = await Api.serverProcesses(props.server.id, current.value);
    if (mine !== generation || !alive) return;
    data.value = result;
    error.value = '';
    stale.value = false;
  } catch (e) {
    if (mine !== generation || !alive) return;
    error.value = safeError(e);
    stale.value = true;
    // An older node will not answer on the next try either.
    if (e?.response?.data?.error === 'node_update_required') return;
  }
  if (!away && !document.hidden) timer = setTimeout(load, 3000);
}
function resume() { if (alive && !away && !document.hidden) load(); }
function shown() { if (document.hidden) clearTimeout(timer); else resume(); }
function pick(key) {
  if (key === current.value) return;
  current.value = key;
  confirming.value = stuck.value = 0;
  load();
}
async function end(p, force) {
  ending.value = p.pid;
  note.value = '';
  try {
    const result = await Api.killServerProcess(props.server.id, {pid: p.pid, name: p.name, force});
    if (result.exited) {
      stuck.value = 0;
      note.value = `已${force ? '强制' : ''}结束 ${p.name}（PID ${p.pid}）。`;
      load();
    } else {
      stuck.value = p.pid;
      note.value = force ? `${p.name}（PID ${p.pid}）仍未退出。` : `${p.name}（PID ${p.pid}）没有在 10 秒内退出，可以强制结束。`;
    }
  } catch (e) {
    note.value = safeError(e);
  } finally {
    ending.value = confirming.value = 0;
  }
}
onMounted(() => { document.addEventListener('visibilitychange', shown); load(); });
onDeactivated(() => { away = true; clearTimeout(timer); });
onActivated(() => { if (away) { away = false; resume(); } });
onBeforeUnmount(() => { alive = false; clearTimeout(timer); document.removeEventListener('visibilitychange', shown); });
</script>
<template>
  <div class="pt" :aria-busy="!data && !error">
    <div class="pt-head">
      <strong>{{ TITLES[current] }}</strong>
      <span class="instance-muted">{{ data ? `共 ${data.count} 个进程 · 每 3 秒刷新` : '' }}</span>
    </div>
    <div class="pt-sorts" role="tablist" aria-label="排序">
      <button v-for="[key, label] in sorts" :key="key" type="button" role="tab" :aria-selected="current === key" @click="pick(key)">{{ label }}</button>
    </div>
    <p v-if="error" class="instance-error pt-error" role="alert">{{ error }}</p>
    <p v-else-if="!data || data.sort !== current" class="instance-muted pt-empty" role="status">正在读取…</p>
    <template v-else>
      <template v-if="current === 'net'">
        <div class="pt-section">网卡</div>
        <div v-for="n in busyNics" :key="n.name" class="pt-rate"><span class="pt-name mono">{{ n.name }}</span><span>↑ {{ rate(n.txRate) }}</span><span>↓ {{ rate(n.rxRate) }}</span></div>
        <p v-if="!busyNics.length" class="instance-muted pt-empty">现在没有流量。</p>
        <p v-if="quietNics" class="instance-muted pt-more">另有 {{ quietNics }} 个网卡现在没有流量。</p>
        <template v-if="busyNetns.length">
          <div class="pt-section">容器 <small class="instance-muted">各自网络里的流量</small></div>
          <div v-for="n in busyNetns" :key="n.name" class="pt-rate"><span class="pt-name" :title="n.name">{{ n.name }}<small class="instance-muted"> · {{ n.pids }} 个进程</small></span><span>↑ {{ rate(n.txRate) }}</span><span>↓ {{ rate(n.rxRate) }}</span></div>
        </template>
        <div class="pt-section">进程 <small class="instance-muted">按 TCP 连接数，系统不按进程统计流量</small></div>
      </template>
      <ul v-if="rows.length" class="pt-rows">
        <li v-for="p in rows" :key="p.pid" class="pt-row" :data-pid="p.pid">
          <div class="pt-who">
            <span class="pt-name" :title="p.command || p.name">{{ p.name }}</span>
            <small class="instance-muted">PID {{ p.pid }}{{ p.user ? ` · ${p.user}` : '' }}</small>
            <small v-if="p.command" class="pt-command mono" :title="p.command">{{ p.command }}</small>
          </div>
          <div class="pt-figures">
            <template v-if="current === 'io'"><span><small>读</small>{{ rate(p.readRate || 0) }}</span><span><small>写</small>{{ rate(p.writeRate || 0) }}</span></template>
            <template v-else-if="current === 'conn' || current === 'net'"><span><small>连接</small>{{ p.conns }}</span><span><small>CPU</small>{{ p.cpu.toFixed(1) }}%</span></template>
            <template v-else><span :class="{'is-key': current === 'cpu'}"><small>CPU</small>{{ p.cpu.toFixed(1) }}%</span><span :class="{'is-key': current === 'mem'}"><small>内存</small>{{ bytes(p.rss) }}</span></template>
          </div>
          <div v-if="canEnd" class="pt-action">
            <span v-if="ending === p.pid" class="instance-muted">正在结束…</span>
            <template v-else-if="confirming === p.pid">
              <button type="button" class="ix-text-button is-danger" @click="end(p, false)">确认结束</button>
              <button type="button" class="ix-text-button" @click="confirming = 0">取消</button>
            </template>
            <button v-else-if="stuck === p.pid" type="button" class="ix-text-button is-danger" @click="end(p, true)">强制结束</button>
            <button v-else type="button" class="ix-text-button" :aria-label="`结束 ${p.name}（PID ${p.pid}）`" :disabled="Boolean(ending)" @click="confirming = p.pid; stuck = 0">结束</button>
          </div>
        </li>
      </ul>
      <p v-else class="instance-muted pt-empty">{{ current === 'io' ? '现在没有进程在读写。' : current === 'conn' || current === 'net' ? '现在没有进程有 TCP 连接。' : '没有进程。' }}</p>
      <p v-if="note" class="pt-note" role="status">{{ note }}</p>
      <p v-if="current === 'cpu'" class="instance-muted pt-more">CPU 按整机算：各行加起来就是上面的 CPU 占用。</p>
      <p v-if="stale" class="instance-muted pt-more">数据可能不是最新的。</p>
    </template>
  </div>
</template>
<style>
.pt { display: flex; flex-direction: column; gap: 8px; min-width: 0; font-size: var(--ix-md); }
.pt-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.pt-head > strong { font-size: var(--ix-lg); font-weight: 600; }
.pt-head > span { font-size: var(--ix-xs); white-space: nowrap; }
.pt-sorts { display: flex; flex-wrap: wrap; gap: 4px; }
.pt-sorts button { min-height: 28px; padding: 2px 10px; border: 1px solid var(--ob-chat-border); border-radius: 7px; background: transparent; color: var(--ob-chat-subtle); font: inherit; font-size: var(--ix-sm); cursor: pointer; }
.pt-sorts button[aria-selected='true'] { border-color: transparent; background: var(--ob-chat-selected); color: var(--ob-chat-text); }
.pt-rows { display: flex; flex-direction: column; margin: 0; padding: 0; list-style: none; }
.pt-row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; gap: 4px 14px; padding: 7px 2px; border-top: 1px solid var(--ob-chat-line); }
.pt-who { display: flex; flex-direction: column; min-width: 0; }
.pt-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
.pt-who small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--ix-xs); }
.pt-command { color: var(--ob-chat-subtle); }
.pt-figures { display: flex; gap: 14px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.pt-figures > span { display: flex; flex-direction: column; align-items: flex-end; min-width: 58px; }
.pt-figures small { color: var(--ob-chat-subtle); font-size: var(--ix-xs); }
.pt-figures .is-key { font-weight: 600; }
.pt-action { display: flex; justify-content: flex-end; gap: 8px; min-width: 40px; white-space: nowrap; }
.pt-action .is-danger { color: var(--ob-danger); }
.pt-section { margin-top: 4px; color: var(--ob-chat-subtle); font-size: var(--ix-xs); font-weight: 600; }
.pt-section small { font-weight: 400; }
.pt-rate { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 14px; padding: 4px 2px; font-variant-numeric: tabular-nums; }
.pt-rate > span + span { min-width: 88px; text-align: right; white-space: nowrap; }
.pt-empty, .pt-more, .pt-error, .pt-note { margin: 0; font-size: var(--ix-sm); }
.pt-note { color: var(--ob-chat-text); }
@media (max-width: 760px) {
  .pt-row { grid-template-columns: minmax(0, 1fr) auto; align-items: start; }
  .pt-who { grid-row: span 2; }
  /* Under the figures, so a row grows by little more than its button. */
  .pt-action { grid-column: 2; justify-content: flex-end; margin-top: -2px; }
  .pt-action button { min-height: 32px; padding-inline: 8px; }
}
</style>
