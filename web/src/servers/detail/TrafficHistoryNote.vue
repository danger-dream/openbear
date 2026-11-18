<script setup>
import { bytes } from '../serverHelpers.js';
defineProps({history: {type: Array, default: () => []}, timezone: {type: String, default: ''}});
</script>
<template>
  <details v-if="history.length" class="dt-note traffic-history" data-traffic-history>
    <summary>部分历史保留原口径 · 查看日期</summary>
    <p>完整小时明细按 {{ timezone }} 汇总；以下缺少明细的历史保留原数字，已包含在图表合计中，不与新口径重复计数。</p>
    <ul>
      <li v-for="(row, i) in history" :key="i">
        {{ row.day }} · {{ row.timezone || '历史口径未记录' }} · 下行 {{ bytes(row.rx) }} / 上行 {{ bytes(row.tx) }}
      </li>
    </ul>
  </details>
</template>
<style scoped>
.traffic-history { overflow-wrap: anywhere; }
.traffic-history summary { cursor: pointer; padding: 8px 0; }
.traffic-history p { margin: 4px 0; }
.traffic-history ul { max-height: 12rem; overflow-y: auto; padding-left: 1.25rem; margin: 6px 0; }
.traffic-history li { margin: 4px 0; }
</style>
