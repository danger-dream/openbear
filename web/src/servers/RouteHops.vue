<script setup>
import { computed } from 'vue';
import { probeErrorText } from './netHelpers.js';
import { regionName, routeHopRows } from './serverHelpers.js';

// One 三网回程 cell's route, hop by hop: in a popover by its cell, or a phone's sheet (named: the
// sheet's title already says the city and carrier).
const props = defineProps({cell: {type: Object, required: true}, named: {type: Boolean, default: true}});
const rows = computed(() => routeHopRows(props.cell.hops));
// A trace the node could not run says why as the network test does (dns_failed: … → 解析域名失败).
const failure = computed(() => props.cell.error ? probeErrorText(props.cell.error) : null);
const hopNet = hop => [hop.asn ? `AS${hop.asn}` : '', hop.org, hop.country ? regionName(hop.country) : ''].filter(Boolean).join(' · ');
</script>
<template>
  <div class="sv-route-hops" data-route-hops>
    <p><strong v-if="named">{{ cell.city }}{{ cell.carrier }}</strong><span>{{ cell.line || (failure ? `失败：${failure.title}` : '线路未知（路由隐藏或国内没有响应）') }}</span>
      <small v-if="!cell.line && failure?.detail" class="instance-muted">{{ failure.detail }}</small>
      <span v-if="cell.rttMs != null" class="instance-muted">{{ Math.round(cell.rttMs) }} ms</span>
      <span v-if="!cell.reached && cell.hops.length" class="instance-muted">未到达目标</span></p>
    <ol v-if="cell.hops.length">
      <li v-for="row in rows" :key="row.key" :data-silent="row.silent || undefined">
        <span class="sv-route-ttl">{{ row.label }}</span>
        <span class="mono">{{ row.ip || '*' }}</span>
        <small class="instance-muted">{{ row.silent ? row.note : hopNet(row) }}</small>
        <small class="sv-route-rtt">{{ row.rttMs != null ? `${row.rttMs} ms` : '' }}</small>
      </li>
    </ol>
    <p v-else class="instance-muted">没有逐跳记录。</p>
  </div>
</template>
