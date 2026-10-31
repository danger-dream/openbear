<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue';
import { ChevronRight } from '@lucide/vue';
import { noAutofill } from '../servers/serverHelpers.js';
import { DdnsApi } from './ddnsApi.js';
import NodePicker from './NodePicker.vue';
import { accountLabel, choice, createBody, DEFAULT_STUN, DEFAULT_URLS, emptyForm, errorMessage, FAILOVER, INTERVALS, lines, METHODS, nodeOf,
  patchBody, previewBody, previewCounts, providerOf, recordToForm, rowKey, TTLS, V6_MODES } from './ddnsHelpers.js';

// 添加解析 / 编辑解析: node → domains → record types → preview → save; the rest is under 高级选项.
const props = defineProps({nodes: {type: Array, required: true}, accounts: {type: Array, required: true}, providers: {type: Array, required: true},
  record: {type: Object, default: null}, serverId: {type: String, default: ''}});
const emit = defineEmits(['saved', 'cancel']);
const edit = computed(() => Boolean(props.record));
const firstNode = () => (props.nodes.find(n => n.serverId === props.serverId) || props.nodes.find(n => n.online && n.ddns) || props.nodes[0] || null);
const form = reactive(props.record ? recordToForm(props.record) : emptyForm(firstNode()?.serverId || '', firstNode()));
const fixedRecord = form.method === 'fixed';
const preview = ref(null), previewing = ref(false), previewError = ref(''), confirmed = ref(new Set()), accountIds = reactive({});
const saving = ref(false), error = ref(''), advanced = ref(edit.value);
let round = 0, timer = null;

const node = computed(() => nodeOf(props.nodes, form.serverId));
const v6Known = computed(() => node.value?.v6 || preview.value?.detected?.v6?.value || '');
const rows = computed(() => preview.value?.rows || []);
const counts = computed(() => previewCounts(rows.value, confirmed.value));
const recordAccount = computed(() => props.accounts.find(a => a.id === props.record?.accountId) || null);
// Accounts the rows will be written to: their provider options and TTL ranges apply.
const targetAccounts = computed(() => {
  if (edit.value) return recordAccount.value ? [recordAccount.value] : [];
  const ids = new Set(rows.value.filter(r => r.action !== 'error').map(r => accountIds[r.name] || r.accountId).filter(Boolean));
  return props.accounts.filter(a => ids.has(a.id));
});
const optionFields = computed(() => {
  const out = new Map();
  for (const account of targetAccounts.value) {
    const provider = providerOf(props.providers, account.type);
    for (const f of provider.options || []) {
      const seen = out.get(f.key);
      if (seen) { if (!seen.providers.includes(provider.name)) seen.providers.push(provider.name); }
      else out.set(f.key, {...f, providers: [provider.name]});
    }
  }
  return [...out.values()];
});
// Every provider the names go to updates only A records (Namecheap): AAAA is not offered.
const v6Unsupported = computed(() => targetAccounts.value.length > 0
  && targetAccounts.value.every(a => !(providerOf(props.providers, a.type).caps?.types || ['A', 'AAAA']).includes('AAAA')));
const ttlHint = computed(() => {
  const caps = targetAccounts.value.map(a => providerOf(props.providers, a.type).caps || {}).filter(c => c.ttlMin);
  if (!caps.length) return '';
  const min = Math.max(...caps.map(c => c.ttlMin)), max = Math.min(...caps.map(c => c.ttlMax || 86400));
  return `服务商允许 ${min}–${max} 秒，超出时自动取最近的值`;
});
const warnings = computed(() => {
  const out = [];
  if (form.method !== 'fixed' && node.value && !node.value.ddns) out.push(['bad', '节点版本过旧，需要升级：在服务器页点“更新节点”后才能为它配置动态域名。']);
  if (form.method === 'cmd' && node.value && !node.value.exec) out.push(['bad', '这台节点关闭了命令执行，不能用命令方式取地址；换一种方式，或在服务器上重新安装节点并开启命令执行。']);
  if (form.method !== 'fixed' && node.value && !node.value.online) out.push(['', '节点当前离线，显示的是最后一次上报的地址。']);
  return out;
});
const blocked = computed(() => warnings.value.some(([tone]) => tone === 'bad'));
const formProblem = computed(() => {
  if (form.method !== 'fixed' && !form.serverId) return '先选择节点';
  if (form.method === 'fixed' && !form.fixed.trim()) return '填写固定值';
  if (form.method === 'url' && !lines(form.urls).length) return '填写至少一个查询地址';
  if (form.method === 'stun' && !lines(form.stun).length) return '填写至少一个 STUN 服务器';
  if (form.method === 'cmd' && !form.cmd.trim()) return '填写命令';
  if (form.types.includes('AAAA') && form.v6Mode !== 'first' && !String(form.v6Value).trim()) return '填写 IPv6 地址选择规则';
  return '';
});
const saveNote = computed(() => {
  if (blocked.value) return '';
  if (formProblem.value) return formProblem.value;
  if (edit.value) return '';
  if (counts.value.errors) return '有无法保存的域名，请先修正';
  if (counts.value.pending) return `${counts.value.pending} 条未确认接管，不会保存`;
  return '';
});
const canSave = computed(() => !saving.value && !blocked.value && !formProblem.value && (edit.value || (!previewing.value && preview.value && !counts.value.errors && counts.value.save > 0)));
const saveLabel = computed(() => edit.value ? '保存' : `保存 ${counts.value.save} 条`);
const howHint = computed(() => {
  const v4 = preview.value?.detected?.v4;
  if (form.method === 'auto') return v4?.method ? `这台节点推荐用${METHODS[v4.method] || v4.method}方式${v4.notes?.length ? '：' + v4.notes.map(n => n.text).join('；') : ''}` : '保存前由系统诊断节点的网络后推荐';
  if (form.method === 'conn') return '使用主控看到的节点连接来源 IP。只在节点直连主控时可用，经接入点或反向代理时不可用。';
  return '';
});
const methods = computed(() => ['auto', 'url', 'nic', 'stun', 'cmd', 'conn', ...(fixedRecord ? ['fixed'] : [])]);

const ACTIONS = {new: ['busy', '新建'], same: ['ok', '无需修改'], takeover: ['warn', '需要接管确认'], exists: ['warn', '已由 OpenBear 管理'], error: ['bad', '出错']};
const rowAccounts = row => (row.accounts || []).map(a => props.accounts.find(x => x.id === a.id) || a);
function confirm(row, on) {
  const next = new Set(confirmed.value);
  if (on) next.add(rowKey(row)); else next.delete(rowKey(row));
  confirmed.value = next;
}
function chooseAccount(row, id) { accountIds[row.name] = id; schedule(0); }

function setMethod(method) {
  form.method = method;
  if (method === 'url' && !form.urls.trim()) form.urls = DEFAULT_URLS.join('\n');
  if (method === 'stun' && !form.stun.trim()) form.stun = DEFAULT_STUN.join('\n');
}
function schedule(ms = 600) {
  clearTimeout(timer);
  round++;
  preview.value = null; previewing.value = false; previewError.value = '';
  confirmed.value = new Set();
  timer = setTimeout(runPreview, ms);
}
async function runPreview() {
  clearTimeout(timer);
  const mine = ++round;
  const body = previewBody(form, accountIds);
  if ((!body.serverId && form.method !== 'fixed') || !body.names.length || !body.types.length || formProblem.value) {
    preview.value = null; previewing.value = false; previewError.value = '';
    return;
  }
  previewing.value = true; previewError.value = '';
  try {
    const data = await DdnsApi.preview(body);
    if (mine !== round) return;
    preview.value = data;
    // A confirmation stays only while the same row still asks for one.
    const asking = new Set((data.rows || []).filter(r => r.action === 'takeover').map(rowKey));
    confirmed.value = new Set([...confirmed.value].filter(k => asking.has(k)));
  } catch (e) {
    if (mine === round) { previewError.value = errorMessage(e, '预览失败'); preview.value = null; }
  } finally { if (mine === round) previewing.value = false; }
}
watch(v6Unsupported, no => { if (no && !edit.value) form.types = form.types.filter(t => t !== 'AAAA'); });
watch(optionFields, fields => { for (const f of fields) if (!(f.key in form.options)) form.options[f.key] = ''; }, {immediate: true});
watch(() => form.serverId, (id, before) => {
  if (edit.value || id === before) return;
  const n = nodeOf(props.nodes, id);
  form.types = n?.v6 ? ['A', 'AAAA'] : ['A'];
  if (form.backupId === id) form.backupId = '';
});
watch(() => [form.serverId, form.names, form.types.join(), form.method, form.urls, form.iface, form.bindIface, form.stun, form.cmd, form.fixed, form.v6Mode, form.v6Value, form.v6Temporary,
  JSON.stringify(form.options)], () => schedule(), {immediate: true, flush: 'sync'});
onBeforeUnmount(() => { round++; clearTimeout(timer); });

async function save() {
  if (!canSave.value) return;
  saving.value = true; error.value = '';
  try {
    if (edit.value) {
      const data = await DdnsApi.updateRecord(props.record.id, patchBody(form));
      emit('saved', {message: '已保存，正在按新的设置同步', record: data.record});
    } else {
      const data = await DdnsApi.addRecords(createBody(form, rows.value, confirmed.value, accountIds));
      const saved = data.records?.length || 0, skipped = data.skipped || [];
      const why = skipped.map(s => `${s.name}（${s.type}）${SKIP_REASONS[s.reason] || s.message || '没有保存'}`).join('；');
      emit('saved', {message: `已保存 ${saved} 条，正在写入服务商${skipped.length ? `；${skipped.length} 条没有保存：${why}` : ''}`, records: data.records || []});
    }
  } catch (e) { error.value = errorMessage(e, '保存失败'); }
  finally { saving.value = false; }
}
// Why a row was not saved (POST /ddns/records skipped[].reason); error rows carry their message.
const SKIP_REASONS = {exists: '已经有这条解析', takeover_not_confirmed: '服务商上已有不同的记录，需要先在预览里确认接管'};
defineExpose({busy: saving});
</script>
<template>
  <div class="instance-form dd-form">
    <div class="dd-step">
      <span class="dd-step-n" aria-hidden="true">1</span>
      <div class="dd-step-body">
        <span class="dd-step-title">从哪台节点取地址</span>
        <NodePicker v-if="form.method !== 'fixed'" v-model="form.serverId" :nodes="nodes" label="来源节点" />
        <input v-else v-model="form.fixed" v-bind="noAutofill" class="mono" aria-label="固定值" placeholder="例如 203.0.113.5" />
        <div class="dd-detect" role="status">
          <template v-if="form.method !== 'fixed' && node">
            <div v-if="preview?.detected?.v4 || node.v4" class="dd-detect-row"><span class="dd-detect-lab">IPv4</span><span class="mono">{{ preview?.detected?.v4?.value || node.v4 }}</span>
              <span class="instance-muted">· {{ preview?.detected?.v4 ? `${METHODS[preview.detected.v4.method] || preview.detected.v4.method || '—'}${preview.detected.v4.auto ? '（自动推荐）' : ''}` : '节点上报' }}</span></div>
            <div v-if="v6Known" class="dd-detect-row"><span class="dd-detect-lab">IPv6</span><span class="mono">{{ v6Known }}</span>
              <span class="instance-muted">· {{ preview?.detected?.v6 ? `${METHODS[preview.detected.v6.method] || '网卡'} · 长期地址` : '节点上报' }}</span></div>
            <div v-for="(n, i) in [...(preview?.detected?.v4?.notes || []), ...(preview?.detected?.v6?.notes || [])]" :key="i" class="dd-detect-row" :data-tone="n.level === 'warn' ? 'warn' : ''">{{ n.level === 'warn' ? '⚠ ' : '' }}{{ n.text }}</div>
          </template>
          <div v-for="[tone, text] in warnings" :key="text" class="dd-detect-row" :data-tone="tone">{{ text }}</div>
        </div>
      </div>
    </div>

    <div class="dd-step">
      <span class="dd-step-n" aria-hidden="true">2</span>
      <label class="dd-step-body">
        <span class="dd-step-title">域名</span>
        <textarea v-model="form.names" v-bind="noAutofill" class="mono dd-domains" rows="3" :disabled="edit" spellcheck="false" autocapitalize="off" placeholder="每行一个，例如 home.example.com"></textarea>
        <small v-if="!edit" class="instance-muted">会自动找到能管理这个域的服务商账户；多个账户都能管时，在下面的预览里选。</small>
      </label>
    </div>

    <div class="dd-step">
      <span class="dd-step-n" aria-hidden="true">3</span>
      <div class="dd-step-body">
        <span class="dd-step-title">记录类型</span>
        <div class="dd-checks" role="group" aria-label="记录类型">
          <label class="dd-check"><input v-model="form.types" type="checkbox" value="A" :disabled="edit" />IPv4（A）</label>
          <label class="dd-check"><input v-model="form.types" type="checkbox" value="AAAA" :disabled="edit || v6Unsupported || (!v6Known && !form.types.includes('AAAA'))" />IPv6（AAAA）<small>{{ v6Unsupported ? '这个服务商只能更新 IPv4' : v6Known ? `检测到 ${v6Known}` : '未检测到全局 IPv6' }}</small></label>
        </div>
      </div>
    </div>

    <section class="dd-sec" aria-label="预览">
      <h4>预览<span v-if="previewing" class="instance-muted">　正在诊断节点并读取服务商…</span></h4>
      <div class="dd-preview">
        <div v-if="edit" class="dd-pr">
          <span class="dd-pr-name">{{ record.name }} · {{ record.type }}</span><span class="ix-pill" data-tone="ok">更新设置</span>
          <span class="dd-pr-meta">{{ accountLabel(recordAccount, providers) }}　→　<span class="mono">{{ preview?.rows?.[0]?.value || record.desired || '—' }}</span></span>
        </div>
        <template v-else-if="rows.length">
          <div v-for="row in rows" :key="rowKey(row)" class="dd-pr" :data-action="row.action">
            <span class="dd-pr-name">{{ row.name }} · {{ row.type }}</span>
            <span class="ix-pill" :data-tone="ACTIONS[row.action]?.[0]">{{ ACTIONS[row.action]?.[1] || row.action }}</span>
            <span v-if="row.action === 'error'" class="dd-pr-meta dd-bad-text">{{ row.error || '无法保存' }}{{ !(row.accounts || []).length ? '。先在左侧添加能管理这个域的服务商账户' : '' }}</span>
            <span v-else class="dd-pr-meta">
              <select v-if="rowAccounts(row).length > 1" class="dd-pr-account" :value="accountIds[row.name] || row.accountId" :aria-label="`${row.name} 写入的账户`" @change="chooseAccount(row, $event.target.value)">
                <option v-for="a in rowAccounts(row)" :key="a.id" :value="a.id">{{ accountLabel(a, providers) }}</option>
              </select>
              <template v-else>{{ accountLabel(rowAccounts(row)[0] || accounts.find(a => a.id === row.accountId), providers) }}</template>
              　→　<span class="mono">{{ row.value || '—' }}</span>
            </span>
            <span v-if="row.action === 'exists'" class="dd-pr-meta">已有这条解析，保存时会跳过</span>
            <label v-if="row.action === 'takeover'" class="dd-take">
              <input type="checkbox" :checked="confirmed.has(rowKey(row))" @change="confirm(row, $event.target.checked)" />
              <span>服务商上已有这条记录，当前值 <span class="mono">{{ row.remote || '—' }}</span>{{ row.remoteCount > 1 ? `（同名共 ${row.remoteCount} 条）` : '' }}。确认由 OpenBear 接管，并改为 <span class="mono">{{ row.value || '—' }}</span></span>
            </label>
          </div>
        </template>
        <div v-else class="dd-pr"><span class="dd-pr-meta">{{ previewError || (previewing ? '正在预览…' : '填写域名并勾选记录类型后，这里会显示将要写入的内容') }}</span></div>
      </div>
      <p v-if="previewError && rows.length" class="instance-error">{{ previewError }}</p>
    </section>

    <details class="dd-adv" :open="advanced" @toggle="advanced = $event.target.open">
      <summary><ChevronRight :size="15" class="dd-adv-chev" aria-hidden="true" />高级选项<span class="instance-muted">（取 IP 方式、TTL、服务商选项、冲突与备用来源）</span></summary>
      <div class="dd-adv-grid">
        <div class="dd-field dd-full">
          <span>取 IP 方式（IPv4）</span>
          <div class="dd-choices" role="radiogroup" aria-label="取 IP 方式">
            <label v-for="m in methods" :key="m"><input type="radio" name="dd-method" :value="m" :checked="form.method === m" @change="setMethod(m)" />{{ m === 'auto' ? '自动（推荐）' : METHODS[m] }}</label>
          </div>
          <small v-if="howHint" class="instance-muted">{{ howHint }}</small>
        </div>
        <label v-if="form.method === 'url'" class="dd-field dd-full"><span>查询地址（每行一个，依次尝试）</span>
          <textarea v-model="form.urls" v-bind="noAutofill" class="mono" rows="2" spellcheck="false" autocapitalize="off"></textarea>
          <small class="instance-muted">不走代理，按 IPv4 / IPv6 分别连接</small></label>
        <label v-if="form.method === 'stun'" class="dd-field dd-full"><span>STUN 服务器（每行一个）</span>
          <textarea v-model="form.stun" v-bind="noAutofill" class="mono" rows="2" spellcheck="false" autocapitalize="off"></textarea></label>
        <label v-if="form.method === 'url' || form.method === 'stun'" class="dd-field"><span>绑定网卡（可选）</span>
          <input v-model="form.bindIface" v-bind="noAutofill" class="mono" placeholder="例如 eth0" autocapitalize="off" /></label>
        <label v-if="form.method === 'nic'" class="dd-field"><span>网卡</span>
          <input v-model="form.iface" v-bind="noAutofill" class="mono" placeholder="留空：有默认路由的网卡" autocapitalize="off" /></label>
        <label v-if="form.method === 'cmd'" class="dd-field dd-full"><span>命令（输出里第一个合法地址即为结果）</span>
          <input v-model="form.cmd" v-bind="noAutofill" class="mono" placeholder="例如 curl -s http://192.168.1.1/wan_ip" autocapitalize="off" maxlength="1000" /></label>
        <label class="dd-field"><span>IPv6 地址选择</span>
          <select v-model="form.v6Mode"><option v-for="[v, l] in V6_MODES" :key="v" :value="v">{{ l }}</option></select></label>
        <label v-if="form.v6Mode !== 'first'" class="dd-field"><span>{{ {prefix: '前缀', regex: '正则', index: '第几个（从 1 开始）'}[form.v6Mode] }}</span>
          <input v-model="form.v6Value" v-bind="noAutofill" class="mono" :inputmode="form.v6Mode === 'index' ? 'numeric' : undefined" autocapitalize="off"
            :placeholder="{prefix: '例如 2001:db8:1f0:', regex: '例如 ::51$', index: '例如 2'}[form.v6Mode]" /></label>
        <label class="dd-field"><span>探测间隔</span>
          <select v-model="form.intervalS"><option v-for="[v, l] in INTERVALS" :key="String(v)" :value="v">{{ l }}</option></select></label>
        <label class="dd-field"><span>TTL</span>
          <select v-model="form.ttl"><option v-for="[v, l] in TTLS" :key="String(v)" :value="v">{{ l }}</option></select>
          <small v-if="ttlHint" class="instance-muted">{{ ttlHint }}</small></label>
        <label class="dd-field"><span>同名多条记录时</span>
          <select v-model="form.multi"><option value="one">只管理接管的那一条</option><option value="all">全部改成同一个值</option></select></label>
        <label v-for="f in optionFields" :key="f.key" class="dd-field"><span>{{ f.label }}<template v-if="optionFields.length && targetAccounts.length > 1">（{{ f.providers.join('、') }}）</template></span>
          <select v-model="form.options[f.key]">
            <option value="">{{ f.kind === 'bool' ? '保持记录原样' : '不设置' }}</option>
            <template v-if="f.kind === 'bool'"><option value="on">开启</option><option value="off">关闭</option></template>
            <option v-for="o in (f.options || []).map(choice)" v-else :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
          <small v-if="f.hint" class="instance-muted">{{ f.hint }}</small></label>
        <label class="dd-field"><span>被外部改动时</span>
          <select v-model="form.onDrift"><option value="revert">告警并改回（默认）</option><option value="alert">只告警</option></select></label>
        <label class="ob-check dd-full"><input v-model="form.allowPrivate" type="checkbox" />
          <span>允许写入内网地址<small class="instance-muted">用于内网 DNS；默认会拒绝内网、CGNAT 和保留地址</small></span></label>
        <label v-if="form.types.includes('AAAA')" class="ob-check dd-full"><input v-model="form.v6Temporary" type="checkbox" />
          <span>允许使用临时（隐私）IPv6 地址<small class="instance-muted">默认只用长期地址；临时地址每隔几小时就会变</small></span></label>
        <div v-if="form.method !== 'fixed'" class="dd-field"><span>备用来源</span>
          <NodePicker v-model="form.backupId" :nodes="nodes" label="备用来源" none="不设置" :exclude="form.serverId" /></div>
        <label v-if="form.method !== 'fixed'" class="dd-field"><span>主来源离线多久后切换</span>
          <select v-model="form.backupAfterS" :disabled="!form.backupId"><option v-for="[v, l] in FAILOVER" :key="v" :value="v">{{ l }}</option></select></label>
      </div>
    </details>

    <p v-if="error" role="alert" class="instance-error">{{ error }}</p>
    <div class="instance-form-footer dd-foot">
      <span class="dd-save-note" role="status">{{ saveNote }}</span>
      <el-button @click="emit('cancel')">取消</el-button>
      <el-button type="primary" :loading="saving" :disabled="!canSave" data-testid="dd-save" @click="save">{{ saveLabel }}</el-button>
    </div>
  </div>
</template>
