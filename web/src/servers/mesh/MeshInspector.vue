<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { X } from '@lucide/vue';
import { mesh, ruleHover } from './meshStore.js';
import { drawnRule, ruleEnds, selectionTitle } from './meshGraph.js';
import { ruleSentence } from './meshHelpers.js';
import MeshChanges from './MeshChanges.vue';
import MeshLink from './MeshLink.vue';
import MeshMember from './MeshMember.vue';
import MeshRule from './MeshRule.vue';
import MeshRules from './MeshRules.vue';
import MeshSettings from './MeshSettings.vue';

// The properties panel: the mesh when nothing is selected, else the selected
// node, link or rule. On phones it sits in a bottom sheet with its own title.
const props = defineProps({sel: {type: Object, default: null}, phone: Boolean, items: {type: Array, default: () => []}, installing: {type: Array, default: () => []}, shownPath: {type: String, default: ''}});
const emit = defineEmits(['select', 'open-server', 'connect', 'path', 'show-path', 'remove']);
const members = computed(() => mesh.work?.members || []);
const member = computed(() => props.sel?.kind === 'node' ? members.value.find(m => m.serverId === props.sel.id) || null : null);
const installing = computed(() => props.sel?.kind === 'node' && !member.value ? props.installing.find(x => x.serverId === props.sel.id) : null);
const regionOf = id => props.items.find(i => i.id === id)?.region || '';
const now = ref(Date.now());
let clock;
onMounted(() => { clock = setInterval(() => { now.value = Date.now(); }, 1000); });
onBeforeUnmount(() => clearInterval(clock));
// Several rules between the same two members share one arrow; picking it lists them here.
const pairRules = computed(() => {
  if (props.sel?.kind !== 'rules') return [];
  const [a, b] = props.sel.id.replace(/^rules:/, '').split('|'), live = members.value.filter(m => !m.removing);
  return (mesh.work?.rules || []).map((r, i) => ({r, i})).filter(({r}) => drawnRule(r, members.value) && ruleEnds(r, members.value).some(e => (e.a === a && e.b === b) || (e.a === b && e.b === a)))
    .map(({r, i}) => ({id: r.id, index: i, text: ruleSentence(r, live), note: r.note}));
});
onBeforeUnmount(() => { ruleHover.id = ''; });
const title = computed(() => selectionTitle(props.sel, mesh.work, props.installing));
</script>
<template>
  <div class="mesh-inspector" :class="{'is-phone': phone}">
    <header v-if="!phone" class="mesh-inspector-head">
      <h2 :title="title">{{ title }}</h2>
      <button v-if="sel" type="button" class="ix-icon-button" aria-label="取消选择" title="取消选择（Esc）" @click="emit('select', null)"><X :size="16" /></button>
    </header>
    <div class="mesh-inspector-body">
      <template v-if="!sel">
        <section class="mesh-section" aria-label="更改">
          <h3>更改</h3>
          <MeshChanges panel @member="id => emit('select', {kind: 'node', id})" />
        </section>
        <section class="mesh-section">
          <h3>访问规则<span class="instance-muted">{{ mesh.work.rules.length ? `${mesh.work.rules.length} 条` : '' }}</span></h3>
          <MeshRules :phone="phone" @select="id => emit('select', {kind: 'rule', id})" />
        </section>
        <section class="mesh-section" aria-label="网格设置">
          <h3>网格设置</h3>
          <MeshSettings />
        </section>
      </template>
      <MeshMember v-else-if="member" :key="member.serverId" :member="member" :phone="phone" :items="items" :region="regionOf(member.serverId)" :tab="sel.tab || ''" :shown-path="shownPath"
        @open-server="emit('open-server', $event)" @removed="emit('select', null)" @select="emit('select', $event)" @connect="emit('connect', $event)" @path="emit('path', $event)" @show-path="emit('show-path', $event)" />
      <div v-else-if="installing" class="mesh-installing-note">
        <p><span class="ob-dot" data-tone="warn">正在安装组网服务</span></p>
        <p class="instance-muted">已用 {{ Math.max(0, Math.round((now - (installing.startedAt || now)) / 1000)) }} 秒。装好后加入草稿，应用后连入网格。</p>
      </div>
      <MeshLink v-else-if="sel.kind === 'edge'" :key="sel.id" :edge-id="sel.id" @remove="emit('remove')" @select="emit('select', $event)" />
      <section v-else-if="sel.kind === 'rules'" class="mesh-pair-rules" aria-label="这两台之间的规则">
        <p class="instance-muted">这两台之间有 {{ pairRules.length }} 条规则，点一条编辑。</p>
        <ol class="mesh-rule-list">
          <li v-for="p in pairRules" :key="p.id" class="mesh-rule">
            <button type="button" class="mesh-rule-text" :aria-label="`编辑第 ${p.index + 1} 条规则`" @click="emit('select', {kind: 'rule', id: p.id})"
              @mouseenter="ruleHover.id = p.id" @mouseleave="ruleHover.id = ''">
              <span><span class="instance-muted">{{ p.index + 1 }}.</span> {{ p.text }}</span><small v-if="p.note" class="instance-muted">{{ p.note }}</small>
            </button>
          </li>
        </ol>
      </section>
      <MeshRule v-else-if="sel.kind === 'rule'" :key="sel.id" :rule-id="sel.id" @remove="emit('remove')" />
      <p v-else class="instance-muted">这台服务器已经不在网格里了。</p>
    </div>
  </div>
</template>
