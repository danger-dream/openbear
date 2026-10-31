<script setup>
import { computed, onBeforeUnmount } from 'vue';
import draggable from 'vuedraggable';
import { dragEdgeScroll } from '../dragEdgeScroll.js';
import { mesh, ruleHover, save } from './meshStore.js';
import { autoRuleWords, errorsUnder, ruleWords, uid } from './meshHelpers.js';

// Allow rules, one sentence each; a rule opens in the panel when picked.
const props = defineProps({phone: Boolean, selected: {type: String, default: ''}});
const emit = defineEmits(['select']);
const rules = computed(() => mesh.work.rules);
const members = computed(() => (mesh.work?.members || []).filter(m => !m.removing));
const problems = i => errorsUnder(mesh.errors, `rules[${i}]`).length;
// Let through whatever the rules say, not editable: the gateway's server reaching instances on a mesh address.
const autoRules = computed(() => mesh.data?.autoRules || []);
// All members on a side and nothing else: the rule follows members joining and leaving.
const isAll = side => Boolean(side?.all) && !side.prefixes?.length && !side.subnets?.length;
const pingAll = computed(() => rules.value.some(r => r.proto === 'icmp' && isAll(r.src) && isAll(r.dst)));

function blank() { return {id: uid('r'), src: {all: false, members: [], tags: [], prefixes: []}, dst: {all: false, members: [], tags: [], subnets: []}, proto: 'any', ports: [], note: ''}; }
function add(rule) { rules.value.push(rule); void save(); emit('select', rule.id); }
function remove(i) { if (ruleHover.id === rules.value[i]?.id) ruleHover.id = ''; rules.value.splice(i, 1); void save(); }
let stopEdgeScroll = () => {};
function startDrag(event) { stopEdgeScroll(); stopEdgeScroll = dragEdgeScroll(event.from); }
function endDrag() { stopEdgeScroll(); stopEdgeScroll = () => {}; void save(); }
onBeforeUnmount(() => { stopEdgeScroll(); ruleHover.id = ''; });
</script>
<template>
  <section class="mesh-rules" aria-label="访问规则">
    <p class="mesh-deny" role="note">未被规则允许的访问一律拒绝；回包自动放行。</p>
    <draggable v-if="rules.length" v-model="mesh.work.rules" item-key="id" handle=".so-grip" :animation="160" ghost-class="so-ghost" tag="ol" class="mesh-rule-list" :disabled="phone" :scroll="false" @start="startDrag" @end="endDrag">
      <template #item="{element: r, index: i}">
        <li class="mesh-rule" :data-selected="selected === r.id || undefined" :data-error="problems(i) > 0">
          <span v-if="!phone" class="so-grip" aria-hidden="true" title="拖动调整顺序"><svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><circle cx="9" cy="5" r="1.5"/><circle cx="15" cy="5" r="1.5"/><circle cx="9" cy="12" r="1.5"/><circle cx="15" cy="12" r="1.5"/><circle cx="9" cy="19" r="1.5"/><circle cx="15" cy="19" r="1.5"/></svg></span>
          <button type="button" class="mesh-rule-text" :aria-label="`编辑第 ${i + 1} 条规则`" @click="emit('select', r.id)"
            @mouseenter="ruleHover.id = r.id" @mouseleave="ruleHover.id = ''" @focus="ruleHover.id = r.id" @blur="ruleHover.id = ''">
            <span v-for="w in [ruleWords(r, members)]" :key="r.id">{{ w[0] }} <b>{{ w[1] }}</b> {{ w[2] }} <b v-if="w[3]">{{ w[3] }}</b>{{ w[3] ? ' 的' : '' }} <b>{{ w[4] }}</b></span>
            <small v-if="r.note" class="instance-muted">{{ r.note }}</small>
            <span v-if="problems(i)" class="pb-tag" data-tone="bad">需修改</span>
          </button>
          <button type="button" class="ix-icon-button mesh-drop" :aria-label="`删除第 ${i + 1} 条规则`" title="删除" @click="remove(i)">✕</button>
        </li>
      </template>
    </draggable>
    <p v-else class="instance-muted mesh-rules-empty">{{ autoRules.length ? '还没有规则：除了下面的自动放行，成员之间互相不能访问。' : '还没有规则：成员之间互相不能访问。' }}</p>
    <div class="mesh-rule-add">
      <button type="button" class="ix-text-button" @click="add(blank())">＋ 添加规则</button>
      <button type="button" class="ix-text-button" :disabled="pingAll" @click="add({...blank(), src: {...blank().src, all: true}, dst: {...blank().dst, all: true}, proto: 'icmp', note: '所有成员互相 ping'})">允许所有成员互相 ping</button>
    </div>
    <template v-if="autoRules.length">
      <p class="mesh-auto-head">自动放行：网关经组网访问实例，随实例变化，不能编辑</p>
      <ol class="mesh-rule-list" aria-label="自动放行">
        <li v-for="a in autoRules" :key="`${a.serverId}:${a.instanceId}`" class="mesh-rule" data-auto>
          <span v-if="!phone" class="mesh-rule-pad" aria-hidden="true"></span>
          <p class="mesh-rule-text">
            <span v-for="w in [autoRuleWords(a, members)]" :key="a.instanceId">{{ w[0] }} <b>{{ w[1] }}</b> {{ w[2] }} <b>{{ w[3] }}</b> 的 <b>{{ w[4] }}</b></span>
            <small class="instance-muted">实例 {{ a.name }}</small>
            <span class="pb-tag is-plain">自动</span>
          </p>
        </li>
      </ol>
    </template>
  </section>
</template>
