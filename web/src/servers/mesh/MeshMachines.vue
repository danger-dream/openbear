<script setup>
import { computed, ref } from 'vue';
import { flag, regionName } from '../serverHelpers.js';
import { mesh } from './meshStore.js';
import { errorMessage, errorsUnder, ipKey, isRelay, isSite, msText, nameOf, nodeAlerts, nodeState, PROGRESS, progressOf, transportLabel, warningItems, WARNINGS_TO_FIX } from './meshHelpers.js';
import { containerOf } from './meshGroups.js';

// Every node in the mesh by its mesh address, one row each: what it is, what it does, how it is connected and how it
// is doing; what needs a look is written under its row.
const props = defineProps({phone: Boolean, items: {type: Array, default: () => []}, joined: {type: String, default: ''}});
const emit = defineEmits(['open', 'join']);
const query = ref(''), filter = ref('all');
const data = computed(() => mesh.data);
const members = computed(() => mesh.work?.members || []);
const live = computed(() => members.value.filter(m => !m.removing));
const relaySet = computed(() => new Set(live.value.filter(isRelay).map(m => m.serverId)));
const regionOf = m => String(m.region || props.items.find(i => i.id === m.serverId)?.region || '').toUpperCase();
const changed = computed(() => new Set((data.value?.draft?.changes || []).map(c => c.serverId).filter(Boolean)));
const lineOf = (kind, a, b) => (data.value?.lines || []).find(l => l.kind === kind && ((l.a === a && l.b === b) || (l.a === b && l.b === a)));

function connection(m) {
  const id = m.serverId;
  if (isRelay(m)) {
    const links = (data.value?.lines || []).filter(l => l.kind === 'relay' && (l.a === id || l.b === id)), up = links.filter(l => l.state === 'up').length;
    const riders = live.value.filter(x => containerOf(x, relaySet.value, data.value?.nodes?.[x.serverId]?.summary?.entry)?.relay === id).length;
    return [links.length ? `${up === links.length ? '' : `${up}/`}${links.length} 条线路` : '还没有线路', riders ? `${riders} 台成员经它接入` : ''].filter(Boolean).join(' · ');
  }
  const c = containerOf(m, relaySet.value, data.value?.nodes?.[id]?.summary?.entry);
  if (!c) return (m.entry?.candidates || []).length ? '接入的中转不在组网里' : '还没有设置怎么连进组网';
  const l = lineOf('entry', id, c.relay);
  const more = m.entry?.mode === 'upstreams' ? (m.entry.candidates || []).filter(x => x.relay !== c.relay && relaySet.value.has(x.relay)).length : 0;
  return [`经 ${nameOf(members.value, c.relay)}${more > 0 ? ` +${more}` : ''}`, transportLabel(l?.transport || m.entry.candidates.find(x => x.relay === c.relay)?.transport), Number.isFinite(l?.rttMs) ? msText(l.rttMs) : ''].filter(Boolean).join(' · ');
}
function features(m) {
  const out = [];
  if (m.gateway?.subnets?.length) out.push(['lan', m.gateway.backup ? '备用网关' : '共享局域网']);
  if (m.proxy?.exit) out.push(['exit', '出口']);
  if (m.proxy?.ports?.length) out.push(['ap', `代理端口 ${m.proxy.ports.map(p => `:${p.port}`).join(' ')}`]);
  return out;
}
// What needs a look: the draft's errors on it, the node's alerts (an exit seen differently only where its public address
// matters) and the controller's warnings that ask for something to be done.
function issues(m, index) {
  if (m.removing) return [];
  const seen = new Set(), out = [];
  const add = (tone, text) => { if (text && !seen.has(text)) { seen.add(text); out.push({tone, text}); } };
  for (const e of errorsUnder(mesh.errors, `members[${index}]`)) add('bad', errorMessage(e));
  for (const a of nodeAlerts(data.value, m, members.value, null, mesh.work)) add('warn', a.text);
  for (const w of warningItems(data.value, `members[${index}]`)) if (WARNINGS_TO_FIX.has(w.code)) add('warn', w.text);
  return out;
}
const all = computed(() => members.value.map((m, index) => {
  const [tone, state] = nodeState(data.value, m.serverId), step = progressOf(data.value, m.serverId), region = regionOf(m);
  const fresh = !data.value?.nodes?.[m.serverId]?.appliedRevision, list = issues(m, index);
  return {m, id: m.serverId, flag: flag(region), region: regionName(region) || '', relay: isRelay(m), role: isRelay(m) ? (m.front?.length ? '前置机' : isSite(m) ? '站点中转' : '中转') : '成员',
    tone: m.removing ? 'off' : list.some(x => x.tone === 'bad') ? 'bad' : list.length ? 'warn' : tone,
    state: m.removing ? '待移除' : step && step.state !== 'done' && data.value?.apply?.running ? PROGRESS[step.state]?.[1] || state : list.length ? '需要注意' : state,
    conn: connection(m), features: features(m), fresh, pending: changed.value.has(m.serverId), issues: list};
}));
const FILTERS = [['all', '全部', () => true], ['relay', '中转', r => r.relay], ['member', '成员', r => !r.relay], ['lan', '共享局域网', r => r.features.some(([k]) => k === 'lan')],
  ['exit', '出口', r => r.features.some(([k]) => k === 'exit')], ['ap', '代理端口', r => r.features.some(([k]) => k === 'ap')],
  ['pending', '有未应用改动', r => r.pending || r.fresh], ['issues', '需要注意', r => r.issues.length > 0]];
const counts = computed(() => Object.fromEntries(FILTERS.map(([k, , f]) => [k, all.value.filter(r => !r.m.removing && f(r)).length])));
const rows = computed(() => {
  const q = query.value.trim().toLowerCase(), f = FILTERS.find(x => x[0] === filter.value)[2];
  return all.value.filter(r => f(r) && (!q || r.m.name.toLowerCase().includes(q) || (r.m.address || '').includes(q) || r.region.includes(q)))
    .sort((a, b) => Number(b.id === props.joined) - Number(a.id === props.joined) || ipKey(a.m.address) - ipKey(b.m.address) || a.m.name.localeCompare(b.m.name, 'zh'));
});
</script>
<template>
  <section class="m3-list-page" aria-label="节点">
    <header class="m3-list-head">
      <h2 class="m3-list-title">节点</h2>
      <span class="ob-grow" />
      <input v-model="query" class="m3-input" placeholder="搜索名称、地址或地区" aria-label="找节点" />
      <el-button type="primary" @click="emit('join')">＋ 加入服务器</el-button>
    </header>
    <p class="m3-lead">点一行进入这个节点的详情页，所有设置都在那里改。<b>角色</b>决定它在组网里的位置（成员只自己用，中转还帮别人转发）；<b>功能</b>是可以单独打开的：共享它所在的局域网、当出口让程序从它出去、开代理端口给程序用。</p>
    <div class="m3-chips m3-filters" role="radiogroup" aria-label="筛选">
      <button v-for="[k, l] in FILTERS" :key="k" type="button" class="m3-chip-btn" role="radio" :aria-checked="filter === k" :aria-pressed="filter === k" @click="filter = k">{{ l }}<span class="m3-faint">{{ counts[k] }}</span></button>
    </div>
    <p v-if="!rows.length" class="instance-muted m3-empty-line">{{ members.length ? '没有符合条件的节点。' : '还没有节点加入组网。' }}</p>
    <ul v-else class="m3-rows m3-rows-nodes" :class="{'is-phone': phone}">
      <li v-if="!phone" aria-hidden="true"><div class="m3-row m3-row-head"><span>名称</span><span>角色</span><span>功能</span><span>组网地址</span><span>怎么连进组网</span><span>状态</span></div></li>
      <li v-for="r in rows" :key="r.id" :data-joined="r.id === joined || undefined" :data-removing="r.m.removing || undefined">
        <button type="button" class="m3-row" :aria-label="`打开 ${r.m.name} 的详情`" @click="emit('open', r.id)">
          <span class="m3-row-name"><span class="ob-dot" :data-tone="r.tone" aria-hidden="true"></span><span v-if="r.flag" class="sv-flag" aria-hidden="true">{{ r.flag }}</span><strong>{{ r.m.name }}</strong><span class="m3-faint">{{ r.region }}</span></span>
          <span class="m3-row-role"><span v-if="r.relay" class="m3-tag" data-kind="relay">{{ r.role }}</span><template v-else>成员</template></span>
          <span class="m3-row-tags"><span v-for="[k, l] in r.features" :key="k" class="m3-tag" :data-kind="k">{{ l }}</span><span v-if="!r.features.length && !phone" class="m3-faint">—</span></span>
          <span class="m3-row-addr mono">{{ r.m.address || '应用后分配' }}</span>
          <span class="m3-row-conn">{{ r.conn }}</span>
          <span class="m3-row-state"><span v-if="r.fresh && !r.m.removing" class="m3-tag" data-kind="warn">待应用</span><span v-else-if="r.pending" class="m3-tag" data-kind="warn">有未应用改动</span>
            <span v-else :class="r.tone === 'bad' ? 'ob-bad' : r.tone === 'warn' ? 'ob-warn' : ''">{{ r.state }}</span></span>
          <span v-if="r.issues.length" class="m3-row-issues"><span v-for="x in r.issues" :key="x.text" :class="x.tone === 'bad' ? 'ob-bad' : 'ob-warn'">{{ x.text }}</span></span>
        </button>
      </li>
    </ul>
  </section>
</template>
