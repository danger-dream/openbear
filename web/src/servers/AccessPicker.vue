<script setup>
import { computed } from 'vue';
import { MESH_ACCESS_ID, meshAccessReason } from './serverHelpers.js';

// Primary and optional backup access point; empty primary means the default.
// mesh: the controller's built-in mesh access point (settings.meshAccess), offered only when given
// (not when adding a server: it is no member yet); meshEligible: this server can use it, else access.meshReason says why.
const props = defineProps({points: {type: Array, required: true}, defaultId: {type: String, default: ''}, modelValue: {type: Object, default: () => ({})},
  mesh: {type: Object, default: null}, meshEligible: Boolean, serverMeshReason: {type: String, default: ''}});
const emit = defineEmits(['update:modelValue']);
const value = computed(() => ({primary: props.modelValue?.primary || '', backup: props.modelValue?.backup || ''}));
const fallback = computed(() => props.points.find(p => p.id === props.defaultId) || props.points[0]);
const primaryId = computed(() => value.value.primary || fallback.value?.id || '');
// Why the mesh cannot be picked here ('' when it can): the controller's side first, then this server's.
const meshReason = computed(() => props.mesh ? meshAccessReason(props.mesh, props.meshEligible, props.serverMeshReason) : '');
// Chosen already: just the name (the note below says why it cannot work); otherwise the reason in the option itself.
const meshName = computed(() => props.mesh?.name || '组网');
const meshLabel = computed(() => `${meshName.value}${meshReason.value ? `（${meshReason.value}）` : ''}`);
const meshChosen = computed(() => value.value.primary === MESH_ACCESS_ID || value.value.backup === MESH_ACCESS_ID);
// The mesh as the only way back: when the mesh breaks the node is cut off. A hint, saving is not stopped.
const noBackup = computed(() => value.value.primary === MESH_ACCESS_ID && !value.value.backup);
function set(key, v) { emit('update:modelValue', {...value.value, [key]: v, ...(key === 'primary' && v === value.value.backup ? {backup: ''} : {})}); }
</script>
<template>
  <div class="ap-pick">
    <label><span>首选</span>
      <select :value="value.primary" aria-label="首选接入点" @change="set('primary', $event.target.value)">
        <option value="">默认（{{ fallback?.name || '未设置' }}）</option>
        <option v-for="p in points" :key="p.id" :value="p.id">{{ p.name }}</option>
        <option v-if="mesh" :value="MESH_ACCESS_ID" :disabled="Boolean(meshReason) && value.primary !== MESH_ACCESS_ID">{{ value.primary === MESH_ACCESS_ID ? meshName : meshLabel }}</option>
      </select>
    </label>
    <label><span>备用</span>
      <select :value="value.backup" aria-label="备用接入点" @change="set('backup', $event.target.value)">
        <option value="">不使用</option>
        <option v-for="p in points.filter(p => p.id !== primaryId)" :key="p.id" :value="p.id">{{ p.name }}</option>
        <option v-if="mesh && primaryId !== MESH_ACCESS_ID" :value="MESH_ACCESS_ID" :disabled="Boolean(meshReason) && value.backup !== MESH_ACCESS_ID">{{ value.backup === MESH_ACCESS_ID ? meshName : meshLabel }}</option>
      </select>
    </label>
    <p v-if="mesh && meshChosen && meshReason" class="ap-note" data-tone="warn">已选的组网现在用不了：{{ meshReason }}。</p>
    <p v-if="mesh && noBackup" class="ap-note" data-tone="warn">组网断开时这台会失联，建议设一个备用。</p>
  </div>
</template>
