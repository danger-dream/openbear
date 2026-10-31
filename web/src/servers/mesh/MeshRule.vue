<script setup>
import { computed, ref, watch } from 'vue';
import { noAutofill } from '../serverHelpers.js';
import { mesh, save } from './meshStore.js';
import { errorMessage, errorsUnder, move, parsePorts, portsText, PROTOCOLS } from './meshHelpers.js';
import MeshField from './MeshField.vue';
import MeshSide from './MeshSide.vue';

// One allow rule: sources, targets, protocol and ports.
const props = defineProps({ruleId: {type: String, required: true}});
const emit = defineEmits(['remove', 'back']);
const rules = computed(() => mesh.work.rules);
const index = computed(() => rules.value.findIndex(r => r.id === props.ruleId));
const rule = computed(() => rules.value[index.value] || null);
const members = computed(() => (mesh.work?.members || []).filter(m => !m.removing));
const tags = computed(() => [...new Set(members.value.flatMap(m => m.tags || []))]);
const problems = computed(() => errorsUnder(mesh.errors, `rules[${index.value}]`).map(errorMessage));
const portText = ref(''), portError = ref('');
watch(() => props.ruleId, () => { portText.value = portsText(rule.value?.ports); portError.value = ''; }, {immediate: true});

function setProto(value) {
  rule.value.proto = value;
  if (value === 'any' || value === 'icmp') { rule.value.ports = []; portText.value = ''; portError.value = ''; }
  void save();
}
function setPorts() {
  const {ports, error} = parsePorts(portText.value);
  portError.value = error;
  if (error) return;
  rule.value.ports = ports;
  void save();
}
function shift(delta) { if (move(rules.value, index.value, delta)) void save(); }
</script>
<template>
  <form v-if="rule" class="instance-form mesh-rule-edit" autocomplete="off" aria-label="编辑规则" @submit.prevent>
    <p class="instance-muted mesh-link-text">第 {{ index + 1 }} 条，共 {{ rules.length }} 条。来源可以访问目标。</p>
    <p v-for="e in problems" :key="e" class="instance-error">{{ e }}</p>
    <MeshField label="来源"><MeshSide :side="rule.src" nets="prefixes" label="来源" :members="members" :tags="tags" @change="save" /></MeshField>
    <MeshField label="目标"><MeshSide :side="rule.dst" nets="subnets" label="目标" :members="members" :tags="tags" @change="save" /></MeshField>
    <MeshField label="协议">
      <div class="mesh-choice" role="radiogroup" aria-label="协议">
        <label v-for="[value, label] in PROTOCOLS" :key="value"><input type="radio" :name="`proto-${rule.id}`" :value="value" :checked="(rule.proto || 'any') === value" @change="setProto(value)" />{{ label }}</label>
      </div>
    </MeshField>
    <MeshField v-if="rule.proto === 'tcp' || rule.proto === 'udp'" label="端口" help="多个端口用逗号分隔，范围用短横线，例如 22, 8000-8100；留空表示所有端口。">
      <input v-model="portText" v-bind="noAutofill" class="mono" inputmode="numeric" :class="{invalid: portError}" placeholder="所有端口" aria-label="端口" @change="setPorts" />
      <small v-if="portError" class="ob-bad">{{ portError }}</small>
    </MeshField>
    <MeshField label="备注">
      <input v-model.trim="rule.note" v-bind="noAutofill" maxlength="80" placeholder="可选" aria-label="备注" @change="save" />
    </MeshField>
    <div class="mesh-panel-foot">
      <button type="button" class="ix-text-button" :disabled="index === 0" @click="shift(-1)">上移</button>
      <button type="button" class="ix-text-button" :disabled="index === rules.length - 1" @click="shift(1)">下移</button>
      <span class="ob-grow" />
      <button type="button" class="ix-text-button ob-danger" @click="emit('remove')">删除规则</button>
    </div>
  </form>
  <p v-else class="instance-muted">这条规则已经不在了。</p>
</template>
