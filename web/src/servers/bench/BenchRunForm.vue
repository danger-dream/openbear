<script setup>
import { computed, ref, watch } from 'vue';
import { Api } from '../../api.js';
import NodePicker from '../../ddns/NodePicker.vue';
import { safeError } from '../serverHelpers.js';
import { defaultOptions, warningsFor } from './benchHelpers.js';

// Start a test (or the 综合评测 suite) on a server. The warnings follow the chosen options, so what a
// run installs, costs in traffic or uploads is in view before 开始评测.
const props = defineProps({catalog: {type: Object, required: true}, servers: {type: Array, required: true}, preset: {type: Object, default: null}});
const emit = defineEmits(['started', 'cancel']);
const SUITE = 'suite';
const serverId = ref(props.preset?.serverId && props.servers.some(s => s.id === props.preset.serverId) ? props.preset.serverId : props.servers[0]?.id || '');
const testId = ref(props.preset?.test || SUITE);
const options = ref({});
const saving = ref(false), error = ref('');

// The servers as the picker lists them: flag, name and address, offline ones marked.
const nodes = computed(() => props.servers.map(s => ({serverId: s.id, name: s.name, region: s.region, online: Boolean(s.live || s.node?.online),
  v4: s.node?.info?.ipv4 || '', v6: s.node?.info?.ipv6 || ''})));
const tests = computed(() => props.catalog.tests || []);
const groups = computed(() => (props.catalog.categories || []).map(c => ({...c, tests: tests.value.filter(t => t.category === c.id)})).filter(g => g.tests.length));
// The suite comes with its own options (the protocol), duration and warnings, so it reads like a test.
const chosen = computed(() => testId.value === SUITE ? {...props.catalog.suite, id: SUITE} : tests.value.find(t => t.id === testId.value));
const warnings = computed(() => warningsFor(chosen.value, options.value));
watch(chosen, test => { options.value = defaultOptions(test); }, {immediate: true});

async function start() {
  if (!serverId.value || !chosen.value || saving.value) return;
  saving.value = true; error.value = '';
  try {
    const body = testId.value === SUITE ? {serverId: serverId.value, suite: true, options: options.value} : {serverId: serverId.value, test: testId.value, options: options.value};
    emit('started', (await Api.startBench(body)).runs || []);
  } catch (e) { error.value = safeError(e); }
  finally { saving.value = false; }
}
defineExpose({busy: saving});
</script>
<template>
  <div class="instance-form dd-form bn-form">
    <div class="dd-field">
      <span>服务器</span>
      <NodePicker v-model="serverId" :nodes="nodes" label="服务器" :outdated="() => false" />
      <small class="instance-muted">{{ servers.length ? '只列出能运行评测的服务器：已接入节点、允许执行命令的 Linux（amd64 或 arm64）。'
        : '没有能运行评测的服务器：需要已接入节点、允许执行命令的 Linux 服务器（amd64 或 arm64）。' }}</small>
    </div>
    <label class="dd-field">
      <span>评测项目</span>
      <select v-model="testId" aria-label="评测项目">
        <option :value="SUITE">{{ catalog.suite.name }}（硬件 + IP + 网络 + 回程）</option>
        <optgroup v-for="g in groups" :key="g.id" :label="g.name">
          <option v-for="t in g.tests" :key="t.id" :value="t.id">{{ t.name }}</option>
        </optgroup>
      </select>
      <small v-if="chosen" class="instance-muted">{{ chosen.summary }}耗时：{{ chosen.duration }}。</small>
    </label>
    <template v-for="o in chosen?.options || []" :key="o.key">
      <label v-if="o.type === 'bool'" class="ob-check bn-check">
        <input v-model="options[o.key]" type="checkbox" />
        <span>{{ o.label }}<small v-if="o.help" class="instance-muted">{{ o.help }}</small></span>
      </label>
      <div v-else-if="o.choices.length <= 4" class="dd-field">
        <span>{{ o.label }}</span>
        <div class="dd-choices" role="radiogroup" :aria-label="o.label">
          <label v-for="c in o.choices" :key="c[0]" :title="c[2] || ''"><input v-model="options[o.key]" type="radio" :value="c[0]" class="bn-radio" />{{ c[1] }}</label>
        </div>
        <small v-if="o.choices.find(c => c[0] === options[o.key])?.[2]" class="instance-muted">{{ o.choices.find(c => c[0] === options[o.key])[2] }}</small>
      </div>
      <label v-else class="dd-field">
        <span>{{ o.label }}</span>
        <select v-model="options[o.key]" :aria-label="o.label">
          <option v-for="c in o.choices" :key="c[0]" :value="c[0]">{{ c[1] }}</option>
        </select>
      </label>
    </template>
    <ul v-if="warnings.length" class="bn-warnings" aria-label="运行前请留意">
      <li v-for="w in warnings" :key="w.text">{{ w.text }}</li>
    </ul>
    <p v-if="error" class="instance-error" role="alert">{{ error }}</p>
    <div class="instance-form-footer dd-foot">
      <span class="dd-save-note"></span>
      <el-button @click="emit('cancel')">取消</el-button>
      <el-button type="primary" :loading="saving" :disabled="!serverId || !chosen" data-testid="bn-start" @click="start">开始评测</el-button>
    </div>
  </div>
</template>
