<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { ChevronRight, Plus, Search, Settings, X } from '@lucide/vue';
import { MOBILE_VIEWPORT_QUERY } from '../mobileViewport.js';
import { ensureFlagFont, flag, noAutofill } from '../servers/serverHelpers.js';
import { usePoll } from '../servers/mesh/usePoll.js';
import { DdnsApi } from './ddnsApi.js';
import { accountLabel, checkText, currentValue, errorMessage, filterRecords, needsAttention, nodeOf, providerMark, providerOf, sourceParts, stateTone } from './ddnsHelpers.js';
import AccountForm from './AccountForm.vue';
import DdnsSheet from './DdnsSheet.vue';
import RecordDetail from './RecordDetail.vue';
import RecordForm from './RecordForm.vue';
import '../instances/instances.css';
import '../servers/servers.css';
import './ddns.css';

// 动态域名, a tab of the servers page: provider accounts on the left, the records on the right;
// details, forms and the import open as a drawer or dialog on a desktop and as the bottom sheet on a
// phone. active: the tab is shown (it polls only then); focus: a link to a machine, a record or the add form.
const props = defineProps({active: {type: Boolean, default: true}, focus: {type: Object, default: null}});
const data = ref(null), loading = ref(true), loadError = ref(''), now = ref(Date.now()), phone = ref(false);
const view = ref({account: 'all', filter: 'all', query: '', server: ''});
const selected = ref(''), sheet = ref(''), editing = ref(null), settingsFor = ref(''), addFor = ref(''), checking = ref(false);
const recordRef = ref(null), accountRef = ref(null), detailRef = ref(null);
let media;

const accounts = computed(() => data.value?.accounts || []);
const records = computed(() => data.value?.records || []);
const providers = computed(() => data.value?.providers || []);
const nodes = computed(() => data.value?.nodes || []);
const context = computed(() => ({nodes: nodes.value, accounts: accounts.value, providers: providers.value}));
const shown = computed(() => filterRecords(records.value, view.value, context.value));
const issueCount = computed(() => records.value.filter(needsAttention).length);
const account = computed(() => accounts.value.find(a => a.id === view.value.account) || null);
const record = computed(() => records.value.find(r => r.id === selected.value) || null);
// What a surface shows stays until it has slid away: closing (or a record deleted under it) must not blank it mid-animation.
const mounted = reactive({}), stamp = reactive({}), held = ref(null);
watch(sheet, value => { if (value) { mounted[value] = true; stamp[value] = (stamp[value] || 0) + 1; } });
watch(record, value => { if (value) held.value = value; }, {immediate: true});
const serverNode = computed(() => nodeOf(nodes.value, view.value.server));
const countOf = id => records.value.filter(r => r.accountId === id).length;
const mark = type => providerMark(providerOf(providers.value, type));
const accountMeta = a => a.state === 'error' ? (a.error || '连接失败').split(/[：:]/)[0] : `${providerOf(providers.value, a.type).name} · ${(a.zones || []).length} 个域`;
const emptyText = computed(() => {
  if (!accounts.value.length) return '还没有服务商账户，先在左侧“添加服务商”';
  if (view.value.query || view.value.server) return '没有匹配的解析';
  if (view.value.filter === 'issue') return '没有需要关注的解析';
  return view.value.account === 'all' ? '还没有解析，点“添加解析”开始' : '这个服务商下还没有解析';
});

async function load() {
  try {
    const next = await DdnsApi.list();
    data.value = next; loadError.value = ''; now.value = Date.now();
    if (view.value.account !== 'all' && !next.accounts?.some(a => a.id === view.value.account)) view.value.account = 'all';
    if (selected.value && !next.records?.some(r => r.id === selected.value) && sheet.value === 'detail') sheet.value = '';
  } catch (e) { loadError.value = errorMessage(e, '读不到动态域名的数据'); }
  finally { loading.value = false; }
}
const poll = usePoll(load, {interval: 10000, active: () => props.active});
const refresh = () => poll.run();

function openRecord(r) { selected.value = r.id; sheet.value = 'detail'; }
function openAdd(serverId = '') { editing.value = null; addFor.value = serverId || view.value.server || ''; sheet.value = 'record'; }
function openEdit() { editing.value = record.value; sheet.value = 'record'; }
function openSettings(id) { settingsFor.value = id; sheet.value = 'account'; }
// A surface closing because another one replaced it must not close the new one.
function closeSheet(name) { if (!name || sheet.value === name) sheet.value = ''; }
function sheetClosed(name) {
  if (sheet.value === name) return;
  mounted[name] = false;
  if (name === 'record') editing.value = null;
  if (name === 'account') settingsFor.value = '';
}
function done(result) {
  sheet.value = '';
  if (result?.message) ElMessage.success(result.message);
  if (result?.account?.id) view.value.account = result.account.id;
  void refresh();
}
function removed(message) { sheet.value = ''; selected.value = ''; ElMessage.success(message); void refresh(); }
function chooseAccount(id) { view.value.account = id; }
function clearServer() { view.value.server = ''; }
async function checkAccount() {
  checking.value = true;
  try {
    const [tone, text] = checkText(await DdnsApi.checkAccount(account.value.id));
    ElMessage({type: tone === 'ok' ? 'success' : 'warning', message: text});
  } catch (e) { ElMessage.error(errorMessage(e, '连接失败')); }
  finally { checking.value = false; void refresh(); }
}
const busy = computed(() => [recordRef, accountRef, detailRef].some(r => Boolean(r.value?.busy)));

// A link to a machine narrows the list to it; one to a record opens that, add the form for the machine.
watch(() => props.focus?.nonce, () => {
  const f = props.focus;
  if (!f) return;
  view.value.server = f.serverId || '';
  if (f.recordId) { selected.value = f.recordId; sheet.value = 'detail'; }
  else if (f.add) openAdd(f.serverId);
}, {immediate: true});
function visible() { if (!document.hidden && props.active) void refresh(); }
function syncPhone() { phone.value = media.matches; }
onMounted(() => {
  ensureFlagFont();
  media = window.matchMedia(MOBILE_VIEWPORT_QUERY);
  syncPhone();
  media.addEventListener?.('change', syncPhone);
  document.addEventListener('visibilitychange', visible);
  void refresh();
});
onBeforeUnmount(() => { media?.removeEventListener?.('change', syncPhone); document.removeEventListener('visibilitychange', visible); });
</script>
<template>
  <section class="dd-ui dd-page" aria-label="动态域名">
    <p v-if="loadError" role="alert" class="instance-error instance-banner">{{ loadError }} <button type="button" class="instance-link" @click="refresh">重试</button></p>

    <div class="dd-body">
      <nav class="dd-providers" aria-label="服务商">
        <h2>服务商</h2>
        <button type="button" class="dd-prov" :aria-current="view.account === 'all'" @click="chooseAccount('all')">
          <span class="dd-logo is-all" aria-hidden="true">全</span>
          <span class="dd-prov-text"><span class="dd-prov-name">全部解析</span><span class="dd-prov-meta">{{ issueCount ? `${issueCount} 条需要关注` : '全部正常' }}</span></span>
          <span class="dd-prov-count">{{ records.length }}</span>
        </button>
        <div class="dd-prov-sep" aria-hidden="true"></div>
        <button v-for="a in accounts" :key="a.id" type="button" class="dd-prov" :class="{'is-bad': a.state === 'error'}" :aria-current="view.account === a.id" :data-account="a.id" @click="chooseAccount(a.id)">
          <span class="dd-logo" :style="{background: mark(a.type).color}" aria-hidden="true">{{ mark(a.type).text }}</span>
          <span class="dd-prov-text"><span class="dd-prov-name">{{ a.alias }}</span><span class="dd-prov-meta" :data-tone="a.state === 'error' ? 'bad' : undefined">{{ accountMeta(a) }}</span></span>
          <span class="dd-prov-count">{{ countOf(a.id) }}</span>
        </button>
        <button type="button" class="dd-prov dd-prov-add" :disabled="!data" @click="sheet = 'account'; settingsFor = ''">
          <span class="dd-logo is-all" aria-hidden="true"><Plus :size="14" /></span><span class="dd-prov-name">添加服务商</span><span></span>
        </button>
      </nav>

      <section class="dd-main">
        <div v-if="account" class="dd-acct-bar">
          <span class="dd-logo" :style="{background: mark(account.type).color}" aria-hidden="true">{{ mark(account.type).text }}</span>
          <b class="dd-acct-name">{{ accountLabel(account, providers) }}</b>
          <span class="ix-pill" :data-tone="account.state === 'error' ? 'bad' : account.state === 'ok' ? 'ok' : 'idle'">{{ account.state === 'error' ? account.error || '连接失败' : account.state === 'ok' ? '连接正常' : '尚未检测' }}</span>
          <span class="dd-zones">{{ (account.zones || []).length ? `可管理：${account.zones.join('、')}` : '还没有列出可管理的域' }}</span>
          <span class="ob-grow" />
          <el-button :loading="checking" @click="checkAccount">测试连接</el-button>
          <button type="button" class="ix-icon-button dd-gear" title="服务商设置" aria-label="服务商设置" @click="openSettings(account.id)"><Settings :size="16" /></button>
        </div>
        <div class="dd-toolbar">
          <label class="dd-search"><Search :size="15" aria-hidden="true" /><input v-model="view.query" v-bind="noAutofill" type="search" placeholder="搜索域名、节点或 IP" aria-label="搜索" /></label>
          <div class="sv-ranges" role="tablist" aria-label="按状态筛选">
            <button type="button" role="tab" :aria-selected="view.filter === 'all'" @click="view.filter = 'all'">全部</button>
            <button type="button" role="tab" :aria-selected="view.filter === 'issue'" @click="view.filter = 'issue'">需要关注</button>
          </div>
          <span v-if="view.server" class="dd-chip"><span class="sv-flag" aria-hidden="true">{{ flag(serverNode?.region) }}</span>{{ serverNode?.name || '已删除的节点' }}
            <button type="button" class="dd-chip-x" aria-label="不再按这台机器筛选" @click="clearServer"><X :size="13" /></button></span>
          <span class="ob-grow" />
          <span class="dd-total">{{ shown.length }} 条</span>
          <span class="dd-actions">
            <el-button type="primary" :disabled="!data" @click="openAdd()"><Plus :size="15" aria-hidden="true" class="dd-btn-icon" />添加解析</el-button>
          </span>
        </div>
        <div class="dd-table-wrap">
          <table v-if="shown.length" class="dd-table">
            <thead><tr><th>域名</th><th>类型</th><th>来源</th><th>当前值</th><th>状态</th><th aria-hidden="true"></th></tr></thead>
            <tbody>
              <tr v-for="r in shown" :key="r.id" tabindex="0" :aria-selected="sheet === 'detail' && selected === r.id" :data-record="r.id" @click="openRecord(r)" @keydown.enter.prevent="openRecord(r)">
                <td class="dd-c-domain"><span class="dd-name">{{ r.name }}</span><span class="dd-type dd-type-inline">{{ r.type }}</span>
                  <span v-if="view.account === 'all'" class="dd-acct">{{ accountLabel(accounts.find(a => a.id === r.accountId), providers) }}</span></td>
                <td class="dd-c-type"><span class="dd-type">{{ r.type }}</span></td>
                <td class="dd-c-src"><span class="dd-src-node">{{ sourceParts(r, nodes).node }}</span> <span class="dd-src-how">· {{ sourceParts(r, nodes).how }}</span></td>
                <td class="dd-c-val"><span class="dd-mobile-src">{{ sourceParts(r, nodes).node }} · {{ sourceParts(r, nodes).how }} · </span><span class="mono">{{ currentValue(r) }}</span></td>
                <td class="dd-c-state"><span class="ix-pill" :data-tone="stateTone(r.state)">{{ r.stateText || r.state }}</span></td>
                <td class="dd-c-go" aria-hidden="true"><ChevronRight :size="15" /></td>
              </tr>
            </tbody>
          </table>
          <p v-else class="instance-empty dd-empty">{{ loading ? '正在读取…' : loadError && !data ? '' : emptyText }}</p>
        </div>
      </section>
    </div>

    <DdnsSheet :model-value="sheet === 'detail' && Boolean(record)" kind="drawer" :phone="phone" :title="held ? held.name : ''" :busy="busy"
      @update:model-value="v => { if (!v) closeSheet('detail'); }" @closed="sheetClosed('detail')">
      <RecordDetail v-if="mounted.detail && held" ref="detailRef" :key="held.id" :record="record || held" :nodes="nodes" :accounts="accounts" :providers="providers" :now="now"
        @edit="openEdit" @changed="refresh" @removed="removed" />
    </DdnsSheet>
    <DdnsSheet :model-value="sheet === 'record'" :phone="phone" :title="editing ? '编辑解析' : '添加解析'" :sub="editing ? `${editing.name} · ${editing.type}` : '选节点、填域名，其余由系统自动判断'" :busy="busy"
      @update:model-value="v => { if (!v) closeSheet('record'); }" @closed="sheetClosed('record')">
      <RecordForm v-if="mounted.record && data" ref="recordRef" :key="stamp.record" :nodes="nodes" :accounts="accounts" :providers="providers" :record="editing" :server-id="addFor" @saved="done" @cancel="closeSheet()" />
    </DdnsSheet>
    <DdnsSheet :model-value="sheet === 'account'" :phone="phone" :title="settingsFor ? '服务商设置' : '添加服务商'" sub="凭据只保存在主控的凭据库里，不会下发到节点" width="600px" :busy="busy"
      @update:model-value="v => { if (!v) closeSheet('account'); }" @closed="sheetClosed('account')">
      <AccountForm v-if="mounted.account && data" ref="accountRef" :key="stamp.account" :providers="providers" :account="accounts.find(a => a.id === settingsFor) || null" :records="countOf(settingsFor)"
        @saved="done" @removed="r => { view.account = 'all'; done(r); }" @cancel="closeSheet()" />
    </DdnsSheet>
  </section>
</template>
