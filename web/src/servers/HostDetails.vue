<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { Api } from '../api.js';
import { timeLabel } from '../instances/instanceHelpers.js';
import { bytes, percent, safeError } from './serverHelpers.js';

// The machine in detail, read from the node when opened.
const props = defineProps({server: {type: Object, required: true}});
const KINDS = {ssd: 'SSD', hdd: 'HDD', nvme: 'NVMe SSD', virtual: '虚拟磁盘'};
const details = ref(null), error = ref(''), loading = ref(false);
let alive = true;

const facts = list => list.filter(([, value]) => value !== '' && value != null && value !== 0);
const system = computed(() => {
  const d = details.value;
  return d ? facts([['主机名', d.hostname], ['系统', d.os], ['内核', d.kernel], ['架构', d.arch], ['虚拟化', d.virtualization],
    ['开机于', d.boot ? timeLabel(d.boot * 1000) : ''], ['时区', d.timezone], ['DNS', (d.dns || []).join('、')]]) : [];
});
const cpu = computed(() => {
  const c = details.value?.cpu || {};
  const counts = [c.sockets > 1 && `${c.sockets} 路`, c.cores && `${c.cores} 核`, c.threads && `${c.threads} 线程`].filter(Boolean).join(' · ');
  const speed = [c.mhz && `${(c.mhz / 1000).toFixed(2)} GHz`, c.maxMhz && c.maxMhz !== c.mhz && `最高 ${(c.maxMhz / 1000).toFixed(2)} GHz`].filter(Boolean).join(' · ');
  return facts([['型号', c.model], ['厂商', c.vendor], ['核心', counts], ['频率', speed], ['缓存', c.cache]]);
});
const board = computed(() => {
  const b = details.value?.board || {};
  return facts([['厂商', b.vendor], ['型号', [b.product, b.version].filter(Boolean).join(' ')], ['BIOS', b.bios]]);
});
const memory = computed(() => {
  const d = details.value;
  return d ? facts([['内存', d.memTotal ? bytes(d.memTotal) : ''], ['交换', d.swapTotal ? bytes(d.swapTotal) : '未启用']]) : [];
});

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const result = await Api.serverDetails(props.server.id);
    if (alive) details.value = result.details;
  } catch (e) {
    if (alive) error.value = safeError(e);
  } finally {
    loading.value = false;
  }
}
onMounted(load);
onBeforeUnmount(() => { alive = false; });
</script>
<template>
  <div class="hd" :aria-busy="loading">
    <div class="hd-bar">
      <span class="instance-muted">{{ loading ? '正在读取…' : details ? '打开时从节点读取' : '' }}</span>
      <button type="button" class="ix-text-button" :disabled="loading" @click="load">刷新</button>
    </div>
    <p v-if="error" class="instance-error" role="alert">{{ error }}</p>
    <template v-if="details">
      <section class="hd-section" aria-label="系统">
        <h4>系统</h4>
        <dl class="hd-facts"><div v-for="[label, value] in system" :key="label"><dt>{{ label }}</dt><dd>{{ value }}</dd></div></dl>
      </section>
      <section class="hd-section" aria-label="处理器">
        <h4>处理器</h4>
        <dl class="hd-facts"><div v-for="[label, value] in cpu" :key="label"><dt>{{ label }}</dt><dd>{{ value }}</dd></div></dl>
        <div v-if="details.cpu?.flags?.length" class="hd-tags"><span v-for="f in details.cpu.flags" :key="f" class="pb-tag is-plain">{{ f }}</span></div>
      </section>
      <section v-if="board.length" class="hd-section" aria-label="主板">
        <h4>主板</h4>
        <dl class="hd-facts"><div v-for="[label, value] in board" :key="label"><dt>{{ label }}</dt><dd>{{ value }}</dd></div></dl>
      </section>
      <section class="hd-section" aria-label="内存">
        <h4>内存</h4>
        <dl class="hd-facts"><div v-for="[label, value] in memory" :key="label"><dt>{{ label }}</dt><dd>{{ value }}</dd></div></dl>
      </section>
      <section v-if="details.disks?.length" class="hd-section" aria-label="硬盘">
        <h4>硬盘</h4>
        <ul class="hd-list">
          <li v-for="d in details.disks" :key="d.name"><span class="hd-main"><b class="mono">{{ d.name }}</b><span v-if="d.model">{{ d.model }}</span></span>
            <span class="hd-side">{{ bytes(d.size) }}<span v-if="KINDS[d.kind]" class="pb-tag is-plain">{{ KINDS[d.kind] }}</span></span></li>
        </ul>
      </section>
      <section v-if="details.mounts?.length" class="hd-section" aria-label="挂载点">
        <h4>挂载点</h4>
        <ul class="hd-list">
          <li v-for="m in details.mounts" :key="m.path" class="hd-mount">
            <span class="hd-main"><b class="mono">{{ m.path }}</b><span class="instance-muted">{{ m.fs }}{{ m.device ? ` · ${m.device}` : '' }}</span></span>
            <span class="hd-side">{{ bytes(m.used) }} / {{ bytes(m.total) }}</span>
            <i class="sv-bar" :style="{'--v': percent(m.used, m.total) + '%'}" aria-hidden="true"></i>
          </li>
        </ul>
      </section>
      <section v-if="details.interfaces?.length" class="hd-section" aria-label="网卡">
        <h4>网卡</h4>
        <ul class="hd-list">
          <li v-for="n in details.interfaces" :key="n.name" class="hd-nic">
            <span class="hd-main"><b class="mono">{{ n.name }}</b>
              <span class="instance-muted">{{ [n.up ? '已启用' : '未启用', n.speed ? `${n.speed >= 1000 ? `${n.speed / 1000} Gbit/s` : `${n.speed} Mbit/s`}` : '', n.mtu ? `MTU ${n.mtu}` : '', n.mac].filter(Boolean).join(' · ') }}</span>
              <span v-for="a in n.addrs || []" :key="a" class="mono hd-addr">{{ a }}</span></span>
            <span v-if="n.rx || n.tx" class="hd-side">↓ {{ bytes(n.rx) }} · ↑ {{ bytes(n.tx) }}</span>
          </li>
        </ul>
      </section>
    </template>
  </div>
</template>
<style>
.hd { display: flex; flex-direction: column; gap: 14px; min-width: 0; font-size: var(--ix-md); }
.hd-bar { display: flex; align-items: center; justify-content: space-between; gap: 10px; font-size: var(--ix-xs); }
.hd-section > h4 { margin: 0 0 6px; color: var(--ob-chat-subtle); font-size: var(--ix-xs); font-weight: 600; }
.hd-facts { display: grid; grid-template-columns: minmax(0, 1fr); gap: 4px; margin: 0; }
.hd-facts > div { display: grid; grid-template-columns: 64px minmax(0, 1fr); gap: 10px; }
.hd-facts dt { color: var(--ob-chat-subtle); }
.hd-facts dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.hd-tags { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; }
.hd-list { display: flex; flex-direction: column; margin: 0; padding: 0; list-style: none; }
.hd-list > li { display: flex; flex-wrap: wrap; align-items: flex-start; justify-content: space-between; gap: 4px 12px; padding: 7px 0; border-top: 1px solid var(--ob-chat-line); }
.hd-list > li:first-child { border-top: 0; padding-top: 0; }
.hd-main { display: flex; flex: 1 1 200px; flex-direction: column; gap: 1px; min-width: 0; overflow-wrap: anywhere; }
.hd-main > b { font-weight: 600; }
.hd-main > .instance-muted { font-size: var(--ix-xs); }
.hd-addr { font-size: var(--ix-xs); }
.hd-side { display: flex; flex: none; align-items: center; gap: 6px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.hd-mount .sv-bar { flex-basis: 100%; }
</style>
