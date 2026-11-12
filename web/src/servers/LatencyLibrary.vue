<script setup>
import { computed, reactive, ref } from 'vue';
import { ElMessage } from 'element-plus';
import { Api } from '../api.js';
import { ok, relativeTime } from '../instances/instanceHelpers.js';
import { safeError } from './serverHelpers.js';
import { addressWay, newAddressId } from './latencyTargets.js';
import LatencyAddressForm from './LatencyAddressForm.vue';

// 监测地址: where a server's 延迟 targets come from (the provinces' three carriers in Zstatic's directory,
// the addresses built in and the user's own, in and out of China) and the three default lists. It edits
// the settings form's latency in place; nothing is kept until the settings are saved.
const props = defineProps({latency: {type: Object, required: true}, catalog: {type: Object, default: () => ({})}});
const PARTS = [['three', '全国三网'], ['cn', '国内'], ['intl', '国外'], ['defaults', '默认']];
const part = ref('three');
const KINDS = {cn: [['dns', 'DNS'], ['ai', 'AI 平台'], ['site', '常用网站']], intl: [['dns', 'DNS'], ['ai', 'AI 平台'], ['service', '常用服务']]};
const GROUPS = [['domestic', '国内服务器', '服务器在中国大陆。'],
  ['optimized', '海外 · 优化线路', '服务器在海外，三网回程有精品或优化线路，或者还没检测。列表里的「三网」只测回程不是普通线路的那几网。'],
  ['plain', '海外 · 普通线路', '服务器在海外，电信、联通、移动的回程都只有普通线路（163、4837、CMNET、CMI 等）。']];
const SHORT = {domestic: '国内', optimized: '海外优化', plain: '海外普通'};
const THREE = 'three';
const max = computed(() => props.catalog.maxTargets || 16);
const maxCustom = computed(() => props.catalog.maxCustom || 32);
const carriers = computed(() => props.catalog.carriers || []);
const provinces = computed(() => props.catalog.provinces || []);
const province = computed(() => provinces.value.find(p => p.name === props.latency.province) || provinces.value[0] || {name: '', code: ''});

// The directory as last shown, kept up to date by 立即更新.
const dir = reactive({directory: props.catalog.directory || {}, updatedAtMs: props.catalog.directoryUpdatedAtMs || 0,
  checkedAtMs: props.catalog.directoryCheckedAtMs || 0, error: props.catalog.directoryError || ''});
const refreshing = ref(false), allProvinces = ref(false);
async function refresh() {
  refreshing.value = true;
  try {
    const r = ok(await Api.refreshLatencyDirectory());
    Object.assign(dir, {directory: r.directory, updatedAtMs: r.directoryUpdatedAtMs, checkedAtMs: r.directoryCheckedAtMs, error: r.directoryError});
    if (!r.refreshed) ElMessage.warning('没能取到节点目录，继续用原来的地址');
    else ElMessage.success(r.changed ? '节点目录已更新，在线节点会改测新地址' : '节点目录已是最新');
  } catch (e) { ElMessage.error(`没能更新：${safeError(e)}`); }
  finally { refreshing.value = false; }
}
const nodeRows = computed(() => provinces.value.map(p => ({...p, nodes: carriers.value.map(c => ({...c, endpoint: dir.directory[p.code]?.[c.id] || ''}))})));
const watched = computed(() => nodeRows.value.find(r => r.code === province.value.code));

// The library: built in, then one's own, by where they are.
const lib = computed(() => [...(props.catalog.extras || []), ...props.latency.custom.map(c => ({...c, kind: 'custom'}))]);
const labelOf = id => id === THREE ? `${province.value.name}三网` : lib.value.find(e => e.id === id)?.label || id;
const inLists = id => GROUPS.filter(([g]) => props.latency.defaults[g].includes(id)).map(([g]) => SHORT[g]);
const sections = group => [...KINDS[group].map(([kind, title]) => ({kind, title, items: lib.value.filter(e => e.group === group && e.kind === kind)})),
  {kind: 'custom', title: '自定义', items: lib.value.filter(e => e.group === group && e.kind === 'custom')}];

// One's own: added and changed in a small form; an id of its own from the start, so a default list can name it before saving.
const editing = ref(null);
function startEdit(group, item = null) { editing.value = {group, id: item?.id || ''}; }
function saveEdit(value) {
  const {group, id} = editing.value;
  const entry = {label: value.label, host: value.host, port: value.port, group, ...(value.mode ? {mode: value.mode} : {}), ...(value.url ? {url: value.url} : {})};
  if (id) props.latency.custom = props.latency.custom.map(c => c.id === id ? {id, ...entry} : c);
  else props.latency.custom = [...props.latency.custom, {id: newAddressId(), ...entry}];
  editing.value = null;
}
const way = e => e.kind === 'custom' ? addressWay(e) : {way: '', where: `${e.host}:${e.port}`};
function removeOwn(id) {
  props.latency.custom = props.latency.custom.filter(c => c.id !== id);
  for (const [g] of GROUPS) props.latency.defaults[g] = props.latency.defaults[g].filter(t => t !== id);
  if (editing.value?.id === id) editing.value = null;
}

// 默认: each list picked from 三网 and the library; a list keeps the order things were picked in.
const open = reactive({domestic: false, optimized: false, plain: false});
function toggle(group, id) {
  const list = props.latency.defaults[group];
  props.latency.defaults[group] = list.includes(id) ? list.filter(t => t !== id) : [...list, id];
}
function restore(group) { props.latency.defaults[group] = [...(props.catalog.defaultLists?.[group] || [])]; }
const pickRows = computed(() => [['三网', [{id: THREE, label: `${province.value.name}三网`, host: '电信、联通、移动'}]],
  ['国内', lib.value.filter(e => e.group === 'cn')], ['国外', lib.value.filter(e => e.group === 'intl')]]);
</script>
<template>
  <div class="sv-lib" data-latency-library>
    <div class="sv-ranges sv-lib-parts" role="tablist" aria-label="监测地址分类">
      <button v-for="[value, label] in PARTS" :key="value" type="button" role="tab" :aria-selected="part === value" @click="part = value">{{ label }}</button>
    </div>

    <template v-if="part === 'three'">
      <p class="sv-lib-lead">观测地是{{ province.name }}（在「延迟」页签里改）。每台服务器测到这个省电信、联通、移动三个节点的延迟，节点地址来自 Zstatic CDN。</p>
      <dl class="sv-lib-state">
        <div><dt>更新策略</dt><dd>每天自动取一次全国 31 个省的节点地址；没取到时每小时重试，期间继续用上次的地址；一次都没取到过时用内置的副本。地址有变化时，在线节点自动改测新地址。</dd></div>
        <div><dt>当前地址</dt><dd>{{ dir.updatedAtMs ? `${relativeTime(dir.updatedAtMs, Date.now())}取到` : '内置副本' }}</dd></div>
        <div><dt>上次检查</dt>
          <dd v-if="!dir.checkedAtMs" class="instance-muted">还没检查过</dd>
          <dd v-else :data-tone="dir.error ? 'bad' : 'ok'">{{ relativeTime(dir.checkedAtMs, Date.now()) }} · {{ dir.error ? `失败：${dir.error}` : '成功' }}</dd>
        </div>
      </dl>
      <div class="sv-lib-acts"><button type="button" class="ix-text-button" :disabled="refreshing" @click="refresh">{{ refreshing ? '更新中…' : '立即更新' }}</button></div>
      <div class="sv-lib-table" role="region" aria-label="三网节点" tabindex="0">
        <table>
          <thead><tr><th>省份</th><th v-for="c in carriers" :key="c.id">{{ c.name }}</th></tr></thead>
          <tbody>
            <tr v-for="row in allProvinces ? nodeRows : [watched].filter(Boolean)" :key="row.code" :data-watched="row.code === province.code || null">
              <th>{{ row.name }}<small v-if="row.code === province.code">观测地</small></th>
              <td v-for="n in row.nodes" :key="n.id">{{ n.endpoint || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <button type="button" class="ix-text-button sv-lib-more" @click="allProvinces = !allProvinces">{{ allProvinces ? '只看观测地' : `查看全部 ${nodeRows.length} 个省` }}</button>
    </template>

    <template v-else-if="part === 'cn' || part === 'intl'">
      <p class="sv-lib-lead">{{ part === 'cn' ? '国内' : '国外' }}可测的地址。哪些默认测，在「默认」里选；单台服务器可以在详情页「延迟与丢包」的「编辑」里从这里挑。多数平台接在 CDN 后面，测到的是离服务器最近的 CDN 节点，反映线路的延迟和稳定，不代表平台本身能不能用。</p>
      <section v-for="s in sections(part)" :key="s.kind" class="sv-lib-group" :data-kind="s.kind">
        <header><h4>{{ s.title }}</h4></header>
        <ul class="sv-lib-list">
          <li v-for="e in s.items" :key="e.id" :data-id="e.id">
            <LatencyAddressForm v-if="editing?.id === e.id" :item="e" submit="确定" @save="saveEdit" @cancel="editing = null" />
            <template v-else>
              <span class="sv-lib-name">{{ e.label }}</span>
              <code class="sv-lib-host" :title="way(e).where"><b v-if="way(e).way" class="sv-lib-way">{{ way(e).way }}</b>{{ way(e).where }}</code>
              <span class="sv-lib-tags"><i v-for="t in inLists(e.id)" :key="t" :title="`默认测：${t}`">{{ t }}</i></span>
              <span v-if="s.kind === 'custom'" class="sv-lib-row-acts">
                <button type="button" class="ix-text-button" :aria-label="`修改 ${e.label}`" @click="startEdit(part, e)">修改</button>
                <button type="button" class="ix-text-button" data-tone="danger" :aria-label="`删除 ${e.label}`" @click="removeOwn(e.id)">删除</button>
              </span>
            </template>
          </li>
          <li v-if="editing && !editing.id && editing.group === part && s.kind === 'custom'">
            <LatencyAddressForm :group="part" @save="saveEdit" @cancel="editing = null" />
          </li>
          <li v-else-if="s.kind === 'custom' && !s.items.length" class="sv-lib-empty instance-muted">还没有自己加的地址。</li>
        </ul>
        <template v-if="s.kind === 'custom' && !(editing && !editing.id && editing.group === part)">
          <button v-if="latency.custom.length < maxCustom" type="button" class="ix-text-button sv-lib-more" @click="startEdit(part)">添加地址</button>
          <small v-else class="instance-muted">国内、国外自己加的地址合计最多 {{ maxCustom }} 个，已经加满。</small>
        </template>
      </section>
    </template>

    <template v-else>
      <p class="sv-lib-lead">「跟随设置」的服务器按所在位置和三网回程，测下面其中一组。单台服务器可以在详情页「延迟与丢包」的「编辑」里换成自己的一组。</p>
      <section v-for="[group, title, note] in GROUPS" :key="group" class="sv-lib-group" :data-group="group">
        <header><h4>{{ title }}</h4><span class="instance-muted">{{ latency.defaults[group].length }} / {{ max }}</span>
          <button type="button" class="ix-text-button" :aria-expanded="open[group]" @click="open[group] = !open[group]">{{ open[group] ? '收起' : '修改' }}</button></header>
        <small class="instance-muted">{{ note }}</small>
        <p class="sv-lib-chosen">{{ latency.defaults[group].length ? `测 ${latency.defaults[group].map(labelOf).join('、')}` : '不测' }}</p>
        <div v-if="open[group]" class="sv-lib-pick">
          <div v-for="[name, items] in pickRows" :key="name" class="sv-lib-pick-row">
            <span>{{ name }}</span>
            <div class="sv-pick" role="group" :aria-label="`${title}：${name}`">
              <button v-for="e in items" :key="e.id" type="button" :aria-pressed="latency.defaults[group].includes(e.id)" :title="e.id === THREE ? e.host : way(e).where"
                :disabled="!latency.defaults[group].includes(e.id) && latency.defaults[group].length >= max" @click="toggle(group, e.id)">{{ e.label }}</button>
            </div>
          </div>
          <button type="button" class="ix-text-button sv-lib-more" @click="restore(group)">恢复内置默认</button>
        </div>
      </section>
    </template>
  </div>
</template>
