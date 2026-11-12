<script setup>
import { computed, ref, watch } from 'vue';
import { flag, regionName } from '../serverHelpers.js';
import { mesh, save } from './meshStore.js';
import { errorMessage, errorsUnder, nameOf, TRANSPORTS, transportLabel } from './meshHelpers.js';
import MeshNodeSelect from './MeshNodeSelect.vue';
import { BIAS_LEVELS, biasLevel, reach, RULE_APPLIES, ruleSentence, SIDE_KINDS } from './meshAuto.js';

// One line rule: its name, what it is for, its two sides and what it sets, each part saved as it changes;
// what it reaches now and some of those lines, below.
const props = defineProps({ruleId: {type: String, required: true}, pairs: {type: Array, default: () => []}, regionOf: {type: Function, required: true}});
const emit = defineEmits(['close']);
const rules = computed(() => mesh.work?.lineRules || []);
const index = computed(() => rules.value.findIndex(r => r.id === props.ruleId));
const r = computed(() => rules.value[index.value] || null);
const members = computed(() => (mesh.work?.members || []).filter(m => !m.removing));
const name = id => nameOf(members.value, id);
const regionText = code => regionName(code) || code;
const regions = computed(() => [...new Set([...members.value.map(m => props.regionOf(m.serverId)).filter(Boolean), ...(r.value?.a.regions || []), ...(r.value?.b.regions || [])])].sort());
const tags = computed(() => [...new Set(members.value.flatMap(m => m.tags || []))].sort());
const errors = computed(() => index.value < 0 ? [] : errorsUnder(mesh.errors, `lineRules[${index.value}]`).map(errorMessage));
const reached = computed(() => r.value ? reach(rules.value, r.value, props.pairs, members.value, props.regionOf) : {lines: [], entries: 0});
const sentence = computed(() => r.value ? ruleSentence(r.value, name, regionText) : '');

const commit = () => void save();
function setKind(side, kind) {
  r.value[side] = kind === 'region' ? {kind, regions: [], not: false} : kind === 'tag' ? {kind, tags: []} : kind === 'machines' ? {kind, ids: []} : {kind: 'any'};
  commit();
}
function toggle(side, key, value) {
  const list = r.value[side][key] || [];
  r.value[side][key] = list.includes(value) ? list.filter(v => v !== value) : [...list, value];
  commit();
}
function use(field, on) {
  r.value[field] = on ? (field === 'order' ? ['udp', 'tcp', 'tls'] : field === 'bias' ? 100 : 'a') : null;
  commit();
}
function moveOrder(i) { const o = r.value.order; o.splice(i - 1, 0, o.splice(i, 1)[0]); commit(); }
function dropOrder(i) { r.value.order.splice(i, 1); commit(); }
function addOrder(t) { r.value.order.push(t); commit(); }
const bias = ref('');
watch(() => r.value?.bias, v => { bias.value = v ?? ''; }, {immediate: true});
function setBias(value) { const n = Math.round(Number(value)); if (value !== '' && Number.isFinite(n)) { r.value.bias = Math.max(-10000, Math.min(10000, n)); commit(); } bias.value = r.value.bias; }
function moveUp() { if (index.value > 0) { rules.value.splice(index.value - 1, 0, rules.value.splice(index.value, 1)[0]); commit(); } }
function remove() { rules.value.splice(index.value, 1); commit(); emit('close'); }
</script>
<template>
  <div v-if="r" class="m3-form m3-lrule">
    <p class="instance-muted">对哪些节点之间的线路或接入、做什么。规则只改自动值；单条线路上手动改过的不受影响。</p>
    <label class="m3-field"><span>名称</span><input v-model.trim="r.name" class="m3-input" maxlength="60" aria-label="规则名称" @change="commit" /></label>
    <div class="m3-field"><span>用于</span>
      <div class="sv-ranges" role="radiogroup" aria-label="用于"><button v-for="[k, l] in RULE_APPLIES" :key="k" type="button" role="radio" :aria-checked="r.applies === k" @click="r.applies = k; commit()">{{ l }}</button></div>
      <small class="instance-muted">成员接入：成员连中转时用的协议。中转线路：两台中转之间。</small></div>
    <div v-for="[side, label] in [['a', '一端'], ['b', '另一端']]" :key="side" class="m3-field"><span>{{ label }}</span>
      <div class="sv-ranges" role="radiogroup" :aria-label="`${label}的范围`"><button v-for="[k, l] in SIDE_KINDS" :key="k" type="button" role="radio" :aria-checked="r[side].kind === k" @click="setKind(side, k)">{{ l }}</button></div>
      <div v-if="r[side].kind === 'region'" class="m3-chips">
        <button v-for="g in regions" :key="g" type="button" class="m3-chip-btn" :aria-pressed="(r[side].regions || []).includes(g)" @click="toggle(side, 'regions', g)"><span class="sv-flag" aria-hidden="true">{{ flag(g) }}</span>{{ regionText(g) }}</button>
        <label class="ob-check m3-not"><input type="checkbox" :checked="Boolean(r[side].not)" @change="r[side].not = $event.target.checked; commit()" /><span>反选：地区不是上面选的</span></label>
      </div>
      <div v-else-if="r[side].kind === 'tag'" class="m3-chips">
        <button v-for="t in tags" :key="t" type="button" class="m3-chip-btn" :aria-pressed="(r[side].tags || []).includes(t)" @click="toggle(side, 'tags', t)">{{ t }}</button>
        <span v-if="!tags.length" class="instance-muted">还没有节点打标签。</span>
      </div>
      <div v-else-if="r[side].kind === 'machines'" class="m3-chips">
        <span v-for="id in r[side].ids || []" :key="id" class="sv-chip">{{ name(id) }}<button type="button" :aria-label="`去掉 ${name(id)}`" @click="toggle(side, 'ids', id)">✕</button></span>
        <MeshNodeSelect :options="members.filter(x => !(r[side].ids || []).includes(x.serverId)).map(m => ({value: m.serverId, label: m.name, region: regionOf(m.serverId)}))"
          placeholder="添加节点…" :aria-label="`${label}：添加节点`" width="200px" @change="id => id && toggle(side, 'ids', id)" />
      </div>
    </div>
    <div class="m3-field"><span>协议顺序</span>
      <label class="ob-check"><input type="checkbox" :checked="r.order != null" @change="use('order', $event.target.checked)" /><span>按这个顺序选协议</span></label>
      <div v-if="r.order" class="m3-order">
        <span v-for="(t, i) in r.order" :key="t" class="m3-orderbox"><span class="m3-no">{{ i + 1 }}</span>{{ transportLabel(t) }}
          <button type="button" class="ix-icon-button" :disabled="!i" title="往前" :aria-label="`${transportLabel(t)} 往前`" @click="moveOrder(i)">↑</button>
          <button type="button" class="ix-icon-button" :disabled="r.order.length < 2" title="去掉" :aria-label="`去掉 ${transportLabel(t)}`" @click="dropOrder(i)">✕</button></span>
        <el-button v-for="[t, l] in TRANSPORTS.filter(([x]) => !r.order.includes(x))" :key="t" size="small" @click="addOrder(t)">＋ {{ l }}</el-button>
      </div>
      <small class="instance-muted">用被连那一端开着的、排在最前的协议。去掉某个协议就是不用它，比如只留 TLS。</small></div>
    <div class="m3-field"><span>选路偏好</span>
      <label class="ob-check"><input type="checkbox" :checked="r.bias != null" @change="use('bias', $event.target.checked)" /><span>调整选路</span></label>
      <div v-if="r.bias != null" class="m3-order">
        <div class="sv-ranges" role="radiogroup" aria-label="选路偏好"><button v-for="[k, l, v] in BIAS_LEVELS" :key="k" type="button" role="radio" :aria-checked="biasLevel(r.bias) === k" @click="r.bias = v; commit()">{{ l }}</button></div>
        <input class="m3-input m3-short mono" type="number" v-model="bias" aria-label="偏好毫秒数" @change="setBias(bias)" /><span class="instance-muted">ms</span>
      </div>
      <small class="instance-muted">选路时把线路延迟加上这个数再比较。少走 +100：有别的路时尽量不走它。</small></div>
    <div class="m3-field"><span>谁主动连</span>
      <label class="ob-check"><input type="checkbox" :checked="r.dialer != null" @change="use('dialer', $event.target.checked)" /><span>指定由哪一端主动连</span></label>
      <div v-if="r.dialer" class="sv-ranges" role="radiogroup" aria-label="谁主动连"><button v-for="[k, l] in [['a', '「一端」主动连'], ['b', '「另一端」主动连']]" :key="k" type="button" role="radio" :aria-checked="r.dialer === k" @click="r.dialer = k; commit()">{{ l }}</button></div>
      <small class="instance-muted">主动连的一端不接受对方连入。比如某些节点的服务商不允许境外连进来，就让它主动连境外。</small></div>
    <p class="m3-callout" data-tone="info">{{ sentence }}<br>现在作用于 <b>{{ reached.lines.length }}</b> 条中转线路、<b>{{ reached.entries }}</b> 个成员接入{{ reached.lines.length ? `，比如：${reached.lines.slice(0, 6).map(p => `${name(p.a)} ⇄ ${name(p.b)}`).join('、')}` : '' }}。</p>
    <small v-for="e in errors" :key="e" class="ob-bad">{{ e }}</small>
    <div class="m3-dialog-foot">
      <el-button type="danger" plain @click="remove">删除规则</el-button>
      <el-button :disabled="index <= 0" @click="moveUp">往上移</el-button>
      <span class="ob-grow" />
      <el-button type="primary" @click="emit('close')">完成</el-button>
    </div>
  </div>
  <p v-else class="instance-muted">这条规则已经删了。</p>
</template>
