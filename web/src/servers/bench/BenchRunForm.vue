<script setup>
import { computed, ref, watch } from 'vue';
import { Api } from '../../api.js';
import NodePicker from '../../ddns/NodePicker.vue';
import { safeError } from '../serverHelpers.js';
import { defaultOptions, warningsFor } from './benchHelpers.js';

// Start tests (and the 综合评测 suite) on a server; each queues its own run and they run one after another.
// The warnings follow the chosen tests and options, so what a run installs, costs in traffic or uploads is
// in view before 开始评测.
const props = defineProps({catalog: {type: Object, required: true}, servers: {type: Array, required: true}, preset: {type: Object, default: null}});
const emit = defineEmits(['started', 'cancel']);
const SUITE = 'suite';
const serverId = ref(props.preset?.serverId && props.servers.some(s => s.id === props.preset.serverId) ? props.preset.serverId : props.servers[0]?.id || '');
const single = props.preset?.test && props.preset.test !== SUITE ? props.preset.test : '';
const useSuite = ref(!single);
const picked = ref(single ? [single] : []);
const values = ref({});
const saving = ref(false), error = ref('');

// The servers as the picker lists them: flag, name and address, offline ones marked.
const nodes = computed(() => props.servers.map(s => ({serverId: s.id, name: s.name, region: s.region, online: Boolean(s.live || s.node?.online),
  v4: s.node?.info?.ipv4 || '', v6: s.node?.info?.ipv6 || ''})));
const tests = computed(() => props.catalog.tests || []);
const groups = computed(() => (props.catalog.categories || []).map(c => ({...c, tests: tests.value.filter(t => t.category === c.id)})).filter(g => g.tests.length));
const suiteTests = computed(() => props.catalog.suite?.tests || []);
const inSuite = id => useSuite.value && suiteTests.value.includes(id);
// An OpenWrt router runs only some tests (catalog.openwrt); the others stay listed, unavailable, with the reason.
const openwrt = computed(() => Boolean(props.servers.find(s => s.id === serverId.value)?.node?.info?.openwrt));
const allowed = id => !openwrt.value || (props.catalog.openwrt?.tests || []).includes(id);
watch(openwrt, on => {
  if (!on) return;
  useSuite.value = false;
  picked.value = picked.value.filter(allowed);
}, {immediate: true});
// What runs: the suite with its own options, duration and warnings, then each other picked test.
const items = computed(() => [
  ...(useSuite.value ? [{...props.catalog.suite, id: SUITE}] : []),
  ...tests.value.filter(t => picked.value.includes(t.id) && !inSuite(t.id)),
]);
const runCount = computed(() => items.value.reduce((n, item) => n + (item.id === SUITE ? suiteTests.value.length : 1), 0));
// An option two chosen items both have (the address family) is set once for both.
const sig = o => JSON.stringify([o.key, o.label, o.type, o.choices, o.default]);
const shared = computed(() => {
  const seen = new Map();
  for (const item of items.value) for (const o of item.options || []) seen.set(sig(o), [...(seen.get(sig(o)) || []), o]);
  return [...seen.values()].filter(list => list.length > 1).map(list => list[0]);
});
const holders = o => items.value.filter(item => (item.options || []).some(x => sig(x) === sig(o)));
const optionGroups = computed(() => {
  const common = new Set(shared.value.map(sig));
  return [
    ...(shared.value.length ? [{id: 'shared', title: '共用选项', summary: `对${shared.value.length === 1 ? holders(shared.value[0]).map(i => i.name).join('、') : '多个评测'}都生效。`,
      options: shared.value, get: o => values.value[holders(o)[0]?.id]?.[o.key], set: (o, v) => holders(o).forEach(item => { values.value[item.id][o.key] = v; })}] : []),
    ...items.value.map(item => ({id: item.id, title: item.name, summary: `${item.summary}耗时：${item.duration}。`,
      options: (item.options || []).filter(o => !common.has(sig(o))), get: o => values.value[item.id]?.[o.key], set: (o, v) => { values.value[item.id][o.key] = v; }})),
  ];
});
const warnings = computed(() => {
  const seen = new Set();
  return items.value.flatMap(item => warningsFor(item, values.value[item.id])).filter(w => !seen.has(w.text) && seen.add(w.text));
});
// A newly chosen item starts on its defaults, and on the current value of an option it shares.
watch(items, list => {
  const next = Object.fromEntries(list.map(item => [item.id, {...defaultOptions(item), ...values.value[item.id]}]));
  for (const o of shared.value) {
    const from = list.find(item => values.value[item.id] && o.key in values.value[item.id]);
    if (from) for (const item of holders(o)) next[item.id][o.key] = values.value[from.id][o.key];
  }
  values.value = next;
}, {immediate: true});

function setSuite(on) {
  useSuite.value = on;
  if (on) picked.value = picked.value.filter(id => !suiteTests.value.includes(id));
}
function toggle(id, on) {
  picked.value = on ? [...picked.value, id] : picked.value.filter(x => x !== id);
}
function body() {
  const others = items.value.filter(item => item.id !== SUITE).map(item => ({test: item.id, options: values.value[item.id]}));
  if (useSuite.value) return {serverId: serverId.value, suite: true, options: values.value[SUITE], ...(others.length ? {tests: others} : {})};
  return others.length === 1 ? {serverId: serverId.value, test: others[0].test, options: others[0].options} : {serverId: serverId.value, tests: others};
}
async function start() {
  if (!serverId.value || !items.value.length || saving.value) return;
  saving.value = true; error.value = '';
  try {
    emit('started', (await Api.startBench(body())).runs || []);
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
    <div class="dd-field bn-tests" role="group" aria-labelledby="bn-tests-label">
      <span id="bn-tests-label">评测项目<em class="bn-tests-note">可多选</em></span>
      <small v-if="openwrt" class="instance-muted" data-testid="bn-openwrt">{{ catalog.openwrt?.reason }}</small>
      <label class="ob-check bn-test bn-test-suite" :class="{'is-disabled': openwrt}">
        <input type="checkbox" :checked="useSuite" :disabled="openwrt" :aria-label="catalog.suite.name" @change="setSuite($event.target.checked)" />
        <span class="bn-test-name">{{ catalog.suite.name }}<small class="instance-muted">{{ openwrt ? 'OpenWrt 不支持' : '硬件 + IP + 网络 + 回程' }}</small></span>
        <small class="bn-test-time">{{ catalog.suite.duration }}</small>
      </label>
      <div v-for="g in groups" :key="g.id" class="bn-test-group">
        <small class="bn-test-group-name">{{ g.name }}</small>
        <label v-for="t in g.tests" :key="t.id" class="ob-check bn-test" :class="{'is-disabled': inSuite(t.id) || !allowed(t.id)}">
          <input type="checkbox" :checked="inSuite(t.id) || picked.includes(t.id)" :disabled="inSuite(t.id) || !allowed(t.id)" :aria-label="t.name" @change="toggle(t.id, $event.target.checked)" />
          <span class="bn-test-name">{{ t.name }}<small v-if="!allowed(t.id)" class="instance-muted">OpenWrt 不支持</small><small v-else-if="inSuite(t.id)" class="instance-muted">已包含在综合评测里</small></span>
          <small class="bn-test-time">{{ t.duration }}</small>
        </label>
      </div>
      <small class="instance-muted" data-testid="bn-count">{{ runCount ? `共 ${runCount} 个评测，在这台服务器上依次运行。` : '至少选一项。' }}</small>
    </div>
    <section v-for="g in optionGroups" :key="g.id" class="bn-item" :aria-label="g.title">
      <div class="bn-item-head"><b>{{ g.title }}</b><small class="instance-muted">{{ g.summary }}</small></div>
      <template v-for="o in g.options" :key="o.key">
        <label v-if="o.type === 'bool'" class="ob-check bn-check">
          <input type="checkbox" :checked="g.get(o)" @change="g.set(o, $event.target.checked)" />
          <span>{{ o.label }}<small v-if="o.help" class="instance-muted">{{ o.help }}</small></span>
        </label>
        <div v-else-if="o.choices.length <= 4" class="dd-field">
          <span>{{ o.label }}</span>
          <div class="dd-choices" role="radiogroup" :aria-label="o.label">
            <label v-for="c in o.choices" :key="c[0]" :title="c[2] || ''"><input type="radio" :name="`bn-${g.id}-${o.key}`" :checked="g.get(o) === c[0]" class="bn-radio" @change="g.set(o, c[0])" />{{ c[1] }}</label>
          </div>
          <small v-if="o.choices.find(c => c[0] === g.get(o))?.[2]" class="instance-muted">{{ o.choices.find(c => c[0] === g.get(o))[2] }}</small>
        </div>
        <label v-else class="dd-field">
          <span>{{ o.label }}</span>
          <select :value="g.get(o)" :aria-label="o.label" @change="g.set(o, $event.target.value)">
            <option v-for="c in o.choices" :key="c[0]" :value="c[0]">{{ c[1] }}</option>
          </select>
        </label>
      </template>
    </section>
    <ul v-if="warnings.length" class="bn-warnings" aria-label="运行前请留意">
      <li v-for="w in warnings" :key="w.text">{{ w.text }}</li>
    </ul>
    <p v-if="error" class="instance-error" role="alert">{{ error }}</p>
    <div class="instance-form-footer dd-foot">
      <span class="dd-save-note"></span>
      <el-button @click="emit('cancel')">取消</el-button>
      <el-button type="primary" :loading="saving" :disabled="!serverId || !items.length" data-testid="bn-start" @click="start">开始评测</el-button>
    </div>
  </div>
</template>
