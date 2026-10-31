<script setup>
import { computed } from 'vue';
import { Cloud } from '@lucide/vue';

// One server on the board. The ports on its sides start a connection (desktop);
// the board listens for them, this component only draws.
const props = defineProps({id: {type: String, required: true}, data: {type: Object, required: true}});
defineOptions({inheritAttrs: false});
const d = computed(() => props.data);
const nets = computed(() => d.value.subnets || []);
const role = computed(() => [d.value.relay && (d.value.site ? '站点中转' : '中转'), d.value.gateway && '网关'].filter(Boolean));
</script>
<template>
  <div class="mesh-node" :data-tone="d.tone" :data-selected="d.selected || undefined" :data-source="d.source || undefined" :data-target="d.target || undefined"
    :data-installing="d.installing || undefined" :data-removing="d.removing || undefined" :data-relay="d.relay || undefined" :data-dim="d.dim || undefined" :data-path="d.onPath || undefined"
    :data-compact="d.compact || undefined" :title="d.title">
    <div class="mesh-node-title">
      <span v-if="d.flag" class="sv-flag" aria-hidden="true">{{ d.flag }}</span>
      <strong>{{ d.name }}</strong>
      <span class="pb-dot" :data-tone="d.tone" aria-hidden="true"></span>
    </div>
    <div class="mesh-node-meta">
      <span class="mono">{{ d.address || (d.installing ? '' : '应用后分配地址') }}</span>
      <span v-if="d.compact && role.length" class="mesh-node-role">{{ role.join(' · ') }}</span>
      <span v-else-if="d.rtt" class="mesh-node-rtt">{{ d.rtt }}</span>
    </div>
    <div v-if="(!d.compact && role.length) || d.reach || d.alert || d.badge || d.problems || d.removing" class="mesh-node-tags">
      <template v-if="!d.compact"><span v-for="r in role" :key="r" class="pb-tag is-plain">{{ r }}</span></template>
      <span v-if="d.reach" class="pb-tag mesh-node-reach" :data-tone="d.reach[0]" :title="d.reach[2]">{{ d.reach[1] }}</span>
      <span v-if="d.alert" class="pb-tag mesh-node-alert" :data-tone="d.alert[0]" :title="d.alert[2]" role="img" :aria-label="`告警：${d.alert[2]}`">{{ d.alert[1] }}</span>
      <span v-if="d.removing" class="pb-tag" data-tone="bad">待移除</span>
      <span v-else-if="d.badge" class="pb-tag mesh-node-badge" :data-tone="d.badge[0]" :title="d.badge[2] || d.badge[1]">{{ d.badge[1] }}</span>
      <span v-if="d.problems" class="pb-tag" data-tone="bad">需修改</span>
    </div>
    <i v-if="d.installing" class="mesh-node-progress" aria-hidden="true"></i>
    <div v-if="nets.length" class="mesh-node-nets" :title="nets.join('\n')">
      <Cloud :size="12" aria-hidden="true" /><span class="mono">{{ nets[0] }}</span><span v-if="nets.length > 1">+{{ nets.length - 1 }}</span>
    </div>
    <template v-if="d.ports">
      <i v-for="side in ['top', 'right', 'bottom', 'left']" :key="side" class="mesh-port nodrag nopan" :data-side="side" title="拖到另一台服务器上连线"></i>
    </template>
  </div>
</template>
