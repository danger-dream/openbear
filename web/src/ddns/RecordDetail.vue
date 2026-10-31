<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { RefreshCw } from '@lucide/vue';
import { DdnsApi } from './ddnsApi.js';
import { accountLabel, errorMessage, eventTone, METHODS, methodOf, nodeOf, PERMANENT_ERRORS, providerOf, shortTime, sourceParts, stateTone, ttlText } from './ddnsHelpers.js';

// A record's details: what is wrong (if anything), wanted and actual values, where the address comes from,
// a diagnosis of the node's addresses, the event history, and the actions.
const props = defineProps({record: {type: Object, required: true}, nodes: {type: Array, required: true}, accounts: {type: Array, required: true},
  providers: {type: Array, required: true}, now: {type: Number, default: () => Date.now()}});
const emit = defineEmits(['edit', 'changed', 'removed']);
const events = ref(null), eventsError = ref(''), diag = ref(null), diagBusy = ref(false), diagError = ref('');
const busy = ref(''), error = ref(''), removing = ref(false), deleteRemote = ref(false);
let alive = true;

const r = computed(() => props.record);
const account = computed(() => props.accounts.find(a => a.id === r.value.accountId) || null);
const provider = computed(() => account.value ? providerOf(props.providers, account.value.type) : null);
const node = computed(() => nodeOf(props.nodes, r.value.source?.serverId));
const backupNode = computed(() => nodeOf(props.nodes, r.value.backup?.serverId));
const src = computed(() => sourceParts(r.value, props.nodes));
const tone = computed(() => stateTone(r.value.state));
const family = computed(() => r.value.type === 'AAAA' ? 'v6' : 'v4');
const minutes = s => s >= 3600 && s % 3600 === 0 ? `${s / 3600} 小时` : `${Math.round(s / 60)} 分钟`;
const optionText = computed(() => {
  const out = [];
  for (const f of provider.value?.options || []) {
    const value = r.value.options?.[f.key];
    if (value === undefined || value === null || value === '') continue;
    if (typeof value === 'boolean') out.push(`${f.label}：${value ? '开启' : '关闭'}`);
    else out.push(`${f.label}：${(f.options || []).map(o => String(o)).find(o => o.split(':')[0] === String(value))?.split(':').slice(1).join(':') || value}`);
  }
  return out.join('；');
});
// [tone, bold lead, the rest]; nothing for a record that is fine.
const note = computed(() => {
  const rec = r.value, err = rec.error ? `：${rec.error}` : '';
  switch (rec.state) {
    case 'drift': return ['warn', `服务商上的值被改成了 ${rec.remote || '其他值'}`, rec.policy?.onDrift === 'alert'
      ? `已告警；这条解析设置为只告警，不会改回 ${rec.desired}。` : `已告警并改回 ${rec.desired}。如果反复出现，多半是还有别的程序（比如 ddns-go）在写这条记录。`];
    case 'ambiguous': return ['warn', '认不出服务商上哪条同名记录归 OpenBear 管理', `服务商上的同名记录都不是 OpenBear 写入的值，所以没有写入。有多条时，删掉多余的，或把其中一条改成 ${rec.desired || '期望值'}，也可以在“编辑 → 高级选项”里改成“全部改成同一个值”；只剩一条时，OpenBear 管理的那条可能已被删除，剩下这条确实归 OpenBear 管就改成 ${rec.desired || '期望值'}，不是就停止管理或换个名字。`];
    case 'fighting': return ['warn', '另一个系统也在写这条记录', '1 小时内被反复改动。OpenBear 仍会改回，但改回的间隔逐步拉长（最长 1 小时）。停用另一个 DDNS 程序后会恢复正常。'];
    case 'offline': return ['', `来源节点 ${node.value?.name || ''} 离线`, rec.backup?.serverId
      ? `DNS 保持原值不变；离线超过 ${minutes(rec.backup.afterS || 300)} 后改用备用来源 ${backupNode.value?.name || ''}。` : 'DNS 保持原值不变。可以在“编辑 → 高级选项”里设置备用来源。'];
    case 'failed': return ['bad', `同步失败${err}`, PERMANENT_ERRORS.has(rec.errorCode)
      ? '这是服务商账户或配置的问题，不会自动重试；更新凭据或配置后会重新同步，也可以点“立即同步”。' : `会自动重试${rec.failCount ? `（已连续失败 ${rec.failCount} 次）` : ''}。`];
    case 'node_outdated': return ['bad', '节点版本过旧，需要升级', '这台节点还不支持动态域名。在服务器页点“更新节点”，更新后自动开始。'];
    case 'probe_refused': return ['bad', '节点拒绝了取 IP 参数', rec.stateText || '这台节点上新加或改过的探测都没有运行；检查各条解析的取 IP 参数，或在服务器页点“更新节点”。'];
    case 'conn_unavailable': return ['bad', '连接来源地址不可用', '节点不是直连主控，或者连接经过了反向代理，主控看不到它的真实地址。编辑这条解析，换一种取 IP 方式。'];
    case 'no_address': return ['', `没有取到可用的地址${err}`, '节点照常探测，取到地址后自动写入。看看下面的地址诊断。'];
    case 'paused': return ['', '已停用', '不再探测和写入，服务商上的记录保持现状。'];
    case 'syncing': return ['', '正在同步', '正在写入服务商并回读确认…'];
    case 'pending': return ['', '等待同步', rec.stateText || '很快会写入服务商。'];
    default: return null;
  }
});
const facts = computed(() => {
  const rec = r.value;
  return [
    ['期望值（来自来源）', rec.desired || '—', true],
    ['服务商当前值', rec.readable === false ? '服务商不支持读取' : rec.remote || '—', rec.readable !== false, rec.readable !== false && rec.remote && rec.desired && rec.remote !== rec.desired],
    ['来源', `${src.value.node}${src.value.how ? ` · ${src.value.how}` : ''}`],
    ['TTL', ttlText(rec.ttl, provider.value, account.value)],
    ['最近一次变化', shortTime(rec.changedAt, props.now)],
    ['最近一次同步', shortTime(rec.syncedAt, props.now)],
  ];
});
// What the record already knows, until a fresh diagnosis says more.
const known = computed(() => {
  const rec = r.value, s = rec.source || {};
  if (s.kind === 'fixed') return '固定值，不需要节点探测。';
  const how = METHODS[methodOf(rec)] || '自动';
  const parts = [s.methodAuto || s.method === 'auto' ? `自动推荐的方式是${how}。` : `使用${how}方式。`];
  if (rec.policy?.allowPrivate) parts.push('这条解析允许写入内网地址。');
  if (rec.readable === false) parts.push('服务商不支持读取，无法检测外部改动；每 24 小时重写一次。');
  return parts.join('');
});

async function loadEvents() {
  try {
    const data = await DdnsApi.events(r.value.id);
    if (alive) { events.value = data.events || []; eventsError.value = ''; }
  } catch (e) { if (alive) { eventsError.value = errorMessage(e, '读不到事件'); events.value = events.value || []; } }
}
watch(() => [r.value.id, r.value.changedAt, r.value.syncedAt, r.value.state], loadEvents, {immediate: true});
async function diagnose() {
  diagBusy.value = true; diagError.value = '';
  try { diag.value = await DdnsApi.diagnose({serverId: r.value.source.serverId, family: family.value}); }
  catch (e) { diagError.value = errorMessage(e, '诊断失败'); }
  finally { diagBusy.value = false; }
}
async function act(name, fn, done) {
  busy.value = name; error.value = '';
  try { await fn(); done?.(); emit('changed'); }
  catch (e) { error.value = errorMessage(e); }
  finally { busy.value = ''; }
}
const sync = () => act('sync', () => DdnsApi.syncRecord(r.value.id));
const toggle = () => act('pause', () => DdnsApi.updateRecord(r.value.id, {enabled: !r.value.enabled}));
const remove = () => act('remove', () => DdnsApi.deleteRecord(r.value.id, deleteRemote.value), () => emit('removed', deleteRemote.value ? '已删除，并删除了服务商上的记录' : '已删除，服务商上的记录保持原样'));
onBeforeUnmount(() => { alive = false; });
defineExpose({busy});
</script>
<template>
  <div class="dd-detail">
    <div class="dd-detail-meta">
      <span class="dd-type">{{ r.type }}</span>
      <span class="ix-ellipsis">{{ accountLabel(account, providers) }}</span>
      <span class="ix-pill" :data-tone="tone">{{ r.stateText || r.state }}</span>
    </div>
    <div v-if="note" class="dd-note" :data-tone="note[0]"><b>{{ note[1] }}</b>{{ note[2] ? `。${note[2]}` : '' }}</div>
    <div class="dd-kv">
      <div v-for="[k, v, mono, diff] in facts" :key="k"><span class="dd-k">{{ k }}</span><span class="dd-v" :class="{mono, 'is-diff': diff}">{{ v }}</span></div>
      <div v-if="optionText" class="dd-full"><span class="dd-k">服务商选项</span><span class="dd-v">{{ optionText }}</span></div>
      <div v-if="r.backup?.serverId" class="dd-full"><span class="dd-k">备用来源</span><span class="dd-v">{{ backupNode?.name || '已删除的节点' }} · 主来源离线 {{ minutes(r.backup.afterS || 300) }}后切换{{ r.activeSource === 'backup' ? '（正在使用）' : '' }}</span></div>
    </div>

    <section class="dd-sec">
      <h4>地址诊断<button v-if="r.source?.kind !== 'fixed'" type="button" class="ix-text-button dd-sec-action" :disabled="diagBusy" @click="diagnose"><RefreshCw :size="13" aria-hidden="true" />{{ diagBusy ? '诊断中…' : diag ? '重新诊断' : '诊断' }}</button></h4>
      <div class="dd-note">{{ known }}</div>
      <template v-if="diag">
        <ul class="dd-diag">
          <li v-for="(x, i) in diag.results || []" :key="i"><span class="dd-diag-how">{{ METHODS[x.method] || x.method }}</span>
            <span v-if="x.value" class="mono">{{ x.value }}</span><span v-else class="dd-bad-text">{{ x.error || '没有结果' }}</span>
            <span v-if="x.detail" class="instance-muted">{{ x.detail }}</span></li>
          <li v-for="iface in diag.interfaces || []" :key="iface.name"><span class="dd-diag-how">{{ iface.name }}{{ iface.default ? ' · 默认' : '' }}</span>
            <span class="mono">{{ [...(iface.v4 || []), ...(iface.v6 || []).map(a => a.addr + (a.temporary ? '（临时）' : a.deprecated ? '（已过期）' : ''))].join('　') || '—' }}</span></li>
        </ul>
        <p v-if="diag.recommend" class="instance-muted">推荐：{{ METHODS[diag.recommend] || diag.recommend }}</p>
        <div v-for="(n, i) in diag.notes || []" :key="i" class="dd-note" :data-tone="n.level === 'warn' ? 'warn' : ''">{{ n.text }}</div>
      </template>
      <p v-if="diagError" class="instance-error">{{ diagError }}</p>
    </section>

    <section class="dd-sec">
      <h4>事件</h4>
      <p v-if="events === null" class="instance-muted">正在读取…</p>
      <ul v-else-if="events.length" class="dd-events">
        <li v-for="e in events" :key="e.id"><time :title="new Date(e.at).toLocaleString('zh-CN', {hour12: false})">{{ shortTime(e.at, now) }}</time><span :data-tone="eventTone(e.kind)">{{ e.text }}</span></li>
      </ul>
      <p v-else class="instance-muted">{{ eventsError || '还没有事件' }}</p>
    </section>

    <div v-if="removing" class="dd-note" data-tone="bad">
      <p>删除后 OpenBear 不再管理这条解析。</p>
      <label class="ob-check"><input v-model="deleteRemote" type="checkbox" :disabled="provider?.caps?.delete === false" /><span>同时删除服务商上的记录<small class="instance-muted">{{ provider?.caps?.delete === false ? '这家服务商不支持删除记录' : '默认不删除，记录保持现在的值' }}</small></span></label>
    </div>
    <p v-if="error" role="alert" class="instance-error">{{ error }}</p>
    <div class="instance-form-footer dd-foot">
      <template v-if="removing">
        <el-button @click="removing = false; deleteRemote = false">取消</el-button>
        <span class="ob-grow" />
        <el-button type="danger" :loading="busy === 'remove'" @click="remove">{{ deleteRemote ? '删除解析和服务商记录' : '删除解析' }}</el-button>
      </template>
      <template v-else>
        <el-button type="danger" text class="dd-danger" :disabled="Boolean(busy)" @click="removing = true">删除</el-button>
        <span class="ob-grow" />
        <el-button :loading="busy === 'pause'" :disabled="Boolean(busy)" @click="toggle">{{ r.enabled === false ? '启用' : '停用' }}</el-button>
        <el-button :disabled="Boolean(busy)" @click="emit('edit')">编辑</el-button>
        <el-button type="primary" :loading="busy === 'sync'" :disabled="Boolean(busy) || r.enabled === false" @click="sync"><RefreshCw v-if="busy !== 'sync'" :size="14" aria-hidden="true" class="dd-btn-icon" />立即同步</el-button>
      </template>
    </div>
  </div>
</template>
