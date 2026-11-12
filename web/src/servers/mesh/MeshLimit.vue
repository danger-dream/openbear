<script setup>
import { computed } from 'vue';
import { flag, regionName } from '../serverHelpers.js';
import { mesh, save } from './meshStore.js';
import { errorMessage, errorsUnder, nameOf, TRANSPORTS } from './meshHelpers.js';
import MeshNodeSelect from './MeshNodeSelect.vue';
import { limitReach, limitSentence, SIDE_KINDS } from './meshAuto.js';

// One connection limit: its name, its two sides and the conditions a connection across them must meet,
// saved as it changes; what connections cross it now and any draft errors, below.
const props = defineProps({limitId: {type: String, required: true}, pairs: {type: Array, default: () => []}, regionOf: {type: Function, required: true}});
const emit = defineEmits(['close']);
const limits = computed(() => mesh.work?.limits || []);
const index = computed(() => limits.value.findIndex(k => k.id === props.limitId));
const k = computed(() => limits.value[index.value] || null);
const members = computed(() => (mesh.work?.members || []).filter(m => !m.removing));
const name = id => nameOf(members.value, id);
const regionText = code => regionName(code) || code;
const regions = computed(() => [...new Set([...members.value.map(m => props.regionOf(m.serverId)).filter(Boolean), ...(k.value?.a?.regions || []), ...(k.value?.b?.regions || [])])].sort());
const tags = computed(() => [...new Set(members.value.flatMap(m => m.tags || []))].sort());
const errors = computed(() => index.value < 0 ? [] : errorsUnder(mesh.errors, `limits[${index.value}]`).map(errorMessage));
const reached = computed(() => k.value ? limitReach(k.value, props.pairs, mesh.work?.directLinks || [], members.value, props.regionOf) : {lines: [], directs: [], entries: 0, total: 0});
const sentence = computed(() => k.value ? limitSentence(k.value, name, regionText) : '');

const commit = () => void save();
function setKind(side, kind) {
  k.value[side] = kind === 'region' ? {kind, regions: [], not: false} : kind === 'tag' ? {kind, tags: []} : kind === 'machines' ? {kind, ids: []} : {kind: 'any'};
  commit();
}
function toggle(side, key, value) {
  const list = k.value[side][key] || [];
  k.value[side][key] = list.includes(value) ? list.filter(v => v !== value) : [...list, value];
  commit();
}
function useTransports(on) {
  k.value.transports = on ? ['tcp', 'tls'] : null;
  commit();
}
function toggleTransport(t) {
  const cur = k.value.transports || [];
  if (cur.includes(t) && cur.length < 2) return;
  const next = cur.includes(t) ? cur.filter(x => x !== t) : [...cur, t];
  k.value.transports = TRANSPORTS.map(([x]) => x).filter(x => next.includes(x));
  commit();
}
function useVia(on) {
  k.value.via = on ? [] : null;
  commit();
}
function toggleVia(id) {
  const cur = k.value.via || [];
  k.value.via = cur.includes(id) ? cur.filter(x => x !== id) : [...cur, id];
  commit();
}
function remove() { limits.value.splice(index.value, 1); commit(); emit('close'); }
</script>
<template>
  <div v-if="k" class="m3-form m3-lrule">
    <p class="instance-muted">硬规则：两端分别在下面两个范围里的连接，不论手动加的还是自动建的，都必须满足勾选的条件；违反时草稿不能保存。</p>
    <label class="m3-field"><span>名称</span><input v-model.trim="k.name" class="m3-input" maxlength="60" aria-label="限制名称" @change="commit" /></label>
    <div v-for="[side, label] in [['a', '一端'], ['b', '另一端']]" :key="side" class="m3-field"><span>{{ label }}</span>
      <div class="sv-ranges" role="radiogroup" :aria-label="`${label}的范围`"><button v-for="[kind, l] in SIDE_KINDS" :key="kind" type="button" role="radio" :aria-checked="k[side].kind === kind" @click="setKind(side, kind)">{{ l }}</button></div>
      <div v-if="k[side].kind === 'region'" class="m3-chips">
        <button v-for="g in regions" :key="g" type="button" class="m3-chip-btn" :aria-pressed="(k[side].regions || []).includes(g)" @click="toggle(side, 'regions', g)"><span class="sv-flag" aria-hidden="true">{{ flag(g) }}</span>{{ regionText(g) }}</button>
        <label class="ob-check m3-not"><input type="checkbox" :checked="Boolean(k[side].not)" @change="k[side].not = $event.target.checked; commit()" /><span>反选：地区不是上面选的</span></label>
      </div>
      <div v-else-if="k[side].kind === 'tag'" class="m3-chips">
        <button v-for="t in tags" :key="t" type="button" class="m3-chip-btn" :aria-pressed="(k[side].tags || []).includes(t)" @click="toggle(side, 'tags', t)">{{ t }}</button>
        <span v-if="!tags.length" class="instance-muted">还没有节点打标签。</span>
      </div>
      <div v-else-if="k[side].kind === 'machines'" class="m3-chips">
        <span v-for="id in k[side].ids || []" :key="id" class="sv-chip">{{ name(id) }}<button type="button" :aria-label="`去掉 ${name(id)}`" @click="toggle(side, 'ids', id)">✕</button></span>
        <MeshNodeSelect :options="members.filter(x => !(k[side].ids || []).includes(x.serverId)).map(m => ({value: m.serverId, label: m.name, region: regionOf(m.serverId)}))"
          placeholder="添加节点…" :aria-label="`${label}：添加节点`" width="200px" @change="id => id && toggle(side, 'ids', id)" />
      </div>
    </div>
    <div class="m3-field"><span>跨界条件（至少勾一项）</span>
      <label class="ob-check"><input type="checkbox" :checked="Boolean(k.relayEnd)" @change="k.relayEnd = $event.target.checked; commit()" /><span>至少一端是中转（两边的普通成员之间不能直连）</span></label>
      <label class="ob-check"><input type="checkbox" :checked="k.transports != null" @change="useTransports($event.target.checked)" /><span>只允许这些协议</span></label>
      <div v-if="k.transports" class="m3-chips">
        <button v-for="[t, l] in TRANSPORTS" :key="t" type="button" class="m3-chip-btn" :aria-pressed="k.transports.includes(t)" :aria-label="`允许 ${l}`" @click="toggleTransport(t)">{{ l }}</button>
      </div>
      <label class="ob-check"><input type="checkbox" :checked="k.via != null" @change="useVia($event.target.checked)" /><span>只能经过指定节点（跨界连接至少一端须在这些节点里）</span></label>
      <div v-if="k.via != null" class="m3-chips">
        <span v-for="id in k.via" :key="id" class="sv-chip">{{ name(id) }}<button type="button" :aria-label="`去掉指定节点 ${name(id)}`" @click="toggleVia(id)">✕</button></span>
        <MeshNodeSelect :options="members.filter(x => !k.via.includes(x.serverId)).map(m => ({value: m.serverId, label: m.name, region: regionOf(m.serverId)}))"
          placeholder="添加指定节点…" aria-label="只能经过：添加节点" width="220px" @change="id => id && toggleVia(id)" />
      </div>
      <label class="ob-check"><input type="checkbox" :checked="Boolean(k.manualOnly)" @change="k.manualOnly = $event.target.checked; commit()" /><span>只允许手动建立（组网不会自动在两边之间拉线）</span></label>
    </div>
    <p class="m3-callout" data-tone="info">{{ sentence }}<br>现在涉及 <b>{{ reached.total }}</b> 条连接（<b>{{ reached.lines.length }}</b> 条中转线路、<b>{{ reached.entries }}</b> 个成员接入{{ reached.directs.length ? `、${reached.directs.length} 条直连` : '' }}）{{ reached.lines.length ? `，比如：${reached.lines.slice(0, 6).map(p => `${name(p.a)} ⇄ ${name(p.b)}`).join('、')}` : '' }}。</p>
    <small v-for="e in errors" :key="e" class="ob-bad">{{ e }}</small>
    <div class="m3-dialog-foot">
      <el-button type="danger" plain @click="remove">删除限制</el-button>
      <span class="ob-grow" />
      <el-button type="primary" @click="emit('close')">完成</el-button>
    </div>
  </div>
  <p v-else class="instance-muted">这条限制已经删了。</p>
</template>
