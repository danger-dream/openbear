<script setup>
import { computed } from 'vue';
import NodePicker from '../ddns/NodePicker.vue';
import { pickerNode } from './serverHelpers.js';

// Whose node the controller's SSH to this server goes through: none (directly, or a front's exits) or another server's.
const props = defineProps({modelValue: {type: String, default: ''}, servers: {type: Array, default: () => []}, self: {type: String, default: ''}, front: Boolean});
const emit = defineEmits(['update:modelValue']);
// Relays are other servers with a node, not the controller's machine and not fronts; a saved one no longer
// among them stays shown as it is.
const nodes = computed(() => {
  const list = props.servers.filter(s => !s.local && s.id !== props.self && !s.profile?.front && s.node).map(pickerNode);
  return props.modelValue && !list.some(n => n.serverId === props.modelValue)
    ? [...list, {serverId: props.modelValue, name: '已删除或不可用的服务器', region: '', online: false}] : list;
});
</script>
<template>
  <div class="sv-ssh-relay">
    <NodePicker :model-value="modelValue" :nodes="nodes" label="SSH 经由" :none="front ? '经出口（默认）' : '直接连接（默认）'" :outdated="() => false"
      @update:model-value="emit('update:modelValue', $event)" />
  </div>
</template>
