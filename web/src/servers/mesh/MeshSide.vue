<script setup>
import { computed, ref } from 'vue';
import { Close } from '@element-plus/icons-vue';
import { noAutofill } from '../serverHelpers.js';
import { ruleNet, sideParts } from './meshHelpers.js';

// One side of a rule: all members (those joining later too), or chosen members and tags; every gateway
// network (allSubnets, those added later too) and written networks add to either. Nothing chosen covers nobody.
const props = defineProps({side: {type: Object, required: true}, nets: {type: String, default: 'prefixes'}, label: {type: String, required: true},
  members: {type: Array, default: () => []}, tags: {type: Array, default: () => []}});
const emit = defineEmits(['change']);
const net = ref(''), problem = ref(''), picker = ref(null);
const parts = computed(() => sideParts(props.side, props.members, props.nets).filter(p => p.kind !== 'all' && p.kind !== 'allSubnets'));
const chosen = computed(() => Boolean(props.side.all || props.side.allSubnets || parts.value.length));
// What each box covers, in plain words.
const EVERY = {
  prefixes: {all: '各成员自己的组网地址，以后加入的也算', subnets: '各网关后面局域网里的设备，比如家里 10.10.10.0/24 的电脑，可以主动访问目标；以后新加的网段也算'},
  subnets: {all: '各成员自己的组网地址，以后加入的也算；不含网关后面的局域网', subnets: '各网关后面的局域网，比如家里的 10.10.10.0/24；以后新加的网段也算'},
};
const freeMembers = computed(() => props.members.filter(m => !(props.side.members || []).includes(m.serverId)));
const freeTags = computed(() => props.tags.filter(t => !(props.side.tags || []).includes(t)));
// A gateway chosen as a machine stands for its own mesh address only; its networks count once written here.
const bareGateways = computed(() => props.side.allSubnets ? [] : props.members.filter(m => (props.side.members || []).includes(m.serverId) && m.gateway?.subnets?.length
  && !m.gateway.subnets.some(n => (props.side[props.nets] || []).includes(n))));

function add(kind, value) {
  props.side[kind] = [...(props.side[kind] || []), value];
  emit('change');
}
function pick(event) {
  // Only the first colon: a tag may hold more (env:prod).
  const raw = String(event.target.value), at = raw.indexOf(':');
  const kind = raw.slice(0, Math.max(at, 0)), value = at < 0 ? '' : raw.slice(at + 1);
  event.target.value = '';
  if (value) add(kind, value);
}
function addNet() {
  const typed = net.value.trim();
  problem.value = '';
  if (!typed) return;
  // One address is written as its own /32, the way the controller keeps it.
  const {net: value, problem: why} = ruleNet(typed);
  if (why) { problem.value = why; return; }
  net.value = '';
  if (!(props.side[props.nets] || []).includes(value)) add(props.nets, value);
}
// All members stands instead of members and tags (the controller drops them with it); networks stay.
function setAll(on) {
  props.side.all = on;
  if (on) { props.side.members = []; props.side.tags = []; }
  emit('change');
}
// Every gateway network adds to whatever else is chosen; networks written by hand stay.
function setAllSubnets(on) {
  props.side.allSubnets = on;
  emit('change');
}
function remove(part) {
  props.side[part.kind] = props.side[part.kind].filter(v => v !== part.value);
  emit('change');
}
defineExpose({focus: () => picker.value?.focus()});
</script>
<template>
  <div class="ob-ctl mesh-side">
    <div class="mesh-side-every">
      <label class="ob-check mesh-side-all"><input type="checkbox" :checked="Boolean(side.all)" :aria-label="`${label}：所有成员`" @change="setAll($event.target.checked)" />
        <span>所有成员<small class="instance-muted">{{ EVERY[nets].all }}</small></span></label>
      <label class="ob-check mesh-side-all"><input type="checkbox" :checked="Boolean(side.allSubnets)" :aria-label="`${label}：所有网关网段`" @change="setAllSubnets($event.target.checked)" />
        <span>所有网关网段<small class="instance-muted">{{ EVERY[nets].subnets }}</small></span></label>
    </div>
    <div v-if="parts.length || !chosen" class="sv-chips-edit">
      <span v-if="!chosen" class="mesh-all ob-warn">还没有选，这条规则不会生效</span>
      <span v-for="p in parts" :key="p.kind + p.value" class="sv-chip" :class="{mono: p.kind === nets}">{{ p.text }}<button type="button" :aria-label="`从${label}删除 ${p.text}`" @click="remove(p)"><el-icon><Close /></el-icon></button></span>
    </div>
    <div class="ob-inline mesh-side-add">
      <select v-if="!side.all" ref="picker" :aria-label="`${label}：添加成员或标签`" @change="pick">
        <option value="">添加成员或标签…</option>
        <optgroup v-if="freeMembers.length" label="成员"><option v-for="m in freeMembers" :key="m.serverId" :value="`members:${m.serverId}`">{{ m.name }}</option></optgroup>
        <optgroup v-if="freeTags.length" label="标签"><option v-for="t in freeTags" :key="t" :value="`tags:${t}`">{{ t }}</option></optgroup>
      </select>
      <input v-model="net" v-bind="noAutofill" class="mono" :class="{invalid: problem}" :placeholder="side.all ? (nets === 'subnets' ? '另加网关网段，回车添加' : '另加网段，回车添加') : nets === 'subnets' ? '或网关网段，回车添加' : '或网段，回车添加'" :aria-label="`${label}：添加网段`" @keydown.enter.prevent="addNet" @blur="addNet" />
    </div>
    <small v-if="problem" class="ob-bad">{{ problem }}</small>
    <small v-else-if="bareGateways.length" class="instance-muted">{{ bareGateways.map(m => m.name).join('、') }} 只包括这台机器，不包括它的网段；要开放网段请在这里添加。</small>
  </div>
</template>
