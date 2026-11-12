<script setup>
import { CopyDocument, DataAnalysis, InfoFilled } from '@element-plus/icons-vue';
import CardIcon from './CardIcon.vue';
import { computed, defineAsyncComponent, onBeforeUnmount, ref, watch } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Api } from '../../api.js';
import { ok, relativeTime } from '../../instances/instanceHelpers.js';
import { every, useVisible } from './useVisible.js';
import { flag, regionName, safeError } from '../serverHelpers.js';

// IP 质量 laid out as IP lookup sites do: the address, where it is and our 纯净分 on a bar from 高危 to 可信;
// 使用场景 / 类型 beside ASN / 运营商, then 技术指标, IP 情报 and 风险深度检测, each a list of what is known
// with a tag for its verdict; each database's score beside the map, the platforms it unlocks (unless 服务解锁
// has its own card), and what moved since the run before. The IP.Check.Place run (评测) gives most of it; IP
// 情报, read by the controller from public sources, the network's facts, reverse DNS, http:BL and Radar, and
// the open ports when asked to probe them. The map (OpenStreetMap's, IpMap.vue) loads only once the card is
// scrolled to. auto: the server's runs are also started by the controller (IP 质量 in 自动检测;
// app/servers/ip_quality.py IpChecks).
const IpMap = defineAsyncComponent(() => import('./IpMap.vue'));
const props = defineProps({server: {type: Object, required: true}, online: Boolean, active: Boolean, now: {type: Number, required: true},
  unlockShown: Boolean, busy: Boolean, auto: Boolean});
const el = ref(null), data = ref(null), error = ref(''), starting = ref(false), intel = ref(null), scanning = ref(false);
const {seen} = useVisible(el);
let alive = true, mine = 0, wasPending = false;

async function load() {
  const id = ++mine;
  try {
    const result = ok(await Api.serverIpQuality(props.server.id));
    if (!alive || id !== mine) return;
    if (wasPending && !result.pending) {
      if (result.report && result.report.atMs !== data.value?.report?.atMs) ElMessage.success('IP 质量检测完成');
      else ElMessage.warning('IP 质量检测没有得到结果，可以到评测页查看日志');
    }
    wasPending = Boolean(result.pending);
    data.value = result; error.value = '';
  } catch (e) { if (alive && id === mine) error.value = safeError(e); }
}
// Slower (outside sources), so on its own: the rest shows meanwhile.
async function loadIntel() {
  try { const result = ok(await Api.serverIpIntel(props.server.id)); if (alive) intel.value = result; }
  catch { if (alive) intel.value = {failed: true}; }
}
watch(() => seen.value && props.active, on => { if (on) { void load(); if (!intel.value) void loadIntel(); } }, {immediate: true});
// A run under way is followed until it ends.
const stop = every(5000, () => props.active && Boolean(data.value?.pending), load);
onBeforeUnmount(() => { alive = false; stop(); });

async function recheck() {
  try {
    await ElMessageBox.confirm('在这台服务器上运行 IP.Check.Place，大约 1～3 分钟。它会用系统包管理器安装缺少的依赖（jq、curl、bc、dnsutils 等）。',
      '检测 IP 质量', {confirmButtonText: '开始检测', cancelButtonText: '取消', type: 'info'});
  } catch { return; }
  starting.value = true;
  try {
    const result = ok(await Api.startBench({serverId: props.server.id, test: 'ipcheck', options: {family: '4'}}));
    const run = result.runs?.[0];
    data.value = {...(data.value || {report: null, changes: []}), pending: run ? {runId: run.id, state: run.state, startedMs: Date.now()} : {state: 'queued'}};
    wasPending = true;
  } catch (e) { ElMessage.error(safeError(e)); }
  finally { starting.value = false; }
}
async function scan() {
  scanning.value = true;
  try { const result = ok(await Api.scanServerPorts(props.server.id)); intel.value = {...(intel.value || {}), ports: result.ports}; }
  catch (e) { ElMessage.error(safeError(e)); }
  finally { scanning.value = false; }
}
async function copy(text) {
  try { await navigator.clipboard.writeText(text); ElMessage.success(`已复制 ${text}`); } catch { ElMessage.error('复制失败'); }
}

const report = computed(() => data.value?.report || null);
const pending = computed(() => data.value?.pending || null);
const net = computed(() => intel.value?.network || null);
// The address the report is for when that is not the server's IPv4 now: an IPv6 report, or one from before the address changed
// (moved; IP.Check.Place masks it, 203.0.*.*).
const ip = computed(() => {
  const r = report.value;
  if (r?.moved) return r.ip || '';
  if (r?.family === 'IPv6') return props.server.node?.info?.ipv6 || r.ip || '';
  return intel.value?.ip || props.server.node?.info?.ipv4 || '';
});
const SOURCES = {IP2LOCATION: 'IP2Location', SCAMALYTICS: 'Scamalytics', DBIP: 'DB-IP'};
const LEVELS = {low: ['ok', '低'], mid: ['warn', '中'], high: ['bad', '高']};
const level = l => LEVELS[l] || ['off', '未知'];
const scores = computed(() => (report.value?.scores || []).map(s => ({...s, name: SOURCES[s.source] || s.source, tone: level(s.level)[0]})));
const place = computed(() => {
  const loc = report.value?.location || {};
  return [loc.city, loc.subdivision && loc.subdivision !== loc.city ? loc.subdivision : '', loc.countryName || regionName(loc.country)].filter(Boolean).join(' · ');
});
const BAND_TONES = {极度纯净: 'ok', 纯净: 'ok', 一般: 'warn', 存疑: 'bad', 高危: 'bad'};
const purity = computed(() => report.value?.purity ? {...report.value.purity, tone: BAND_TONES[report.value.purity.label] || 'off'} : null);
const NO_PURITY_NOTE = '各家风险库都没有给出评分或判定（多半是查询失败或被限制），没有依据算纯净分。可以稍后重新检测。';
const PURITY_NOTE = '我们自己的算法：100 减去各库风险分（最高一项的一半加平均的五分之一）、各风险因素按判定的库占比扣分'
  + '（代理 25、VPN 20、Tor 30、滥用 25、机器人 10、机房 8）、邮件黑名单（每个 2 分，最多 10）和标记（每个 0.2 分，最多 5）、广播 IP 3 分。';
const SCENE_NOTE = '纯净分除以 10；多数库判为家宽或移动网络加 1 分、机房减 1 分；社媒运营再按判为代理或 VPN 的占比最多减 3 分；'
  + 'TikTok、Reddit、ChatGPT 检测没有解锁时，对应场景最多 3 分（AI 应用最多 2 分）。';
const SCENE_TONES = {极佳: 'ok', 良好: 'ok', 一般: 'warn', 较差: 'bad'};

// What most databases call it: 家宽 by 4 of 5.
function majority(map) {
  const values = Object.values(map || {}), counts = {};
  for (const v of values) counts[v] = (counts[v] || 0) + 1;
  const [top, n] = Object.entries(counts).sort((a, b) => b[1] - a[1])[0] || [];
  return top ? {value: top, n, total: values.length} : null;
}
const USAGES = {家宽: ['家庭住宅 IP', 'ok'], 移动: ['移动网络 IP', 'ok'], 机房: ['机房 IP', null], 商业: ['商业 IP', null], 教育: ['教育网 IP', null], 政府: ['政府 IP', null]};
const usage = computed(() => { const m = majority(report.value?.usage); return m && {...m, text: USAGES[m.value]?.[0] || m.value, tone: USAGES[m.value]?.[1] || null}; });
const company = computed(() => majority(report.value?.company));
const native = computed(() => {
  const r = report.value;
  if (!r?.kind) return null;
  return {native: r.kind.includes('原生'), text: r.kind.includes('原生') ? '原生 IP' : '广播 IP', country: r.location.country, name: r.location.countryName || regionName(r.location.country),
    registered: r.location.registered && r.location.registered !== r.location.country ? r.location.registeredName || regionName(r.location.registered) : ''};
});
// A source of IP 情报: still being read, set up or not, not reachable, or its answer.
const waiting = computed(() => !intel.value);
const missing = value => waiting.value ? '读取中…' : value === null || value === undefined || intel.value?.failed ? '暂时取不到' : '';
const radar = computed(() => net.value?.radar || null);
const PEERING_TYPES = {'Cable/DSL/ISP': '住宅宽带', NSP: '网络服务商', Content: '内容', Enterprise: '企业', 'Educational/Research': '教育科研',
  'Non-Profit': '非营利', 'Route Server': '路由服务器', 'Network Services': '网络服务', 'Route Collector': '路由采集', Government: '政府'};
const asn = computed(() => intel.value?.asn || (report.value?.asn ? Number(report.value.asn) : null));
const RISKY_PORTS = new Set([23, 135, 139, 445, 1433, 2375, 3306, 3389, 5432, 5900, 6379, 9200, 11211, 27017]);
const ports = computed(() => intel.value?.ports || null);
const threats = computed(() => (report.value?.factors || []).filter(f => f.key !== 'Server' && f.flagged));
// No database answered on any factor: unknown, not clean.
const voted = computed(() => (report.value?.factors || []).some(f => f.total));
const abuse = computed(() => {
  const s = (report.value?.scores || []).find(x => x.source === 'AbuseIPDB');
  if (!s || s.value == null) return null;
  return s.value === 0 ? {text: '极度纯净', tone: 'ok'} : {text: `${Math.round(s.value)} · ${level(s.level)[1]}`, tone: level(s.level)[0]};
});
const FACTOR_NAMES = {Proxy: '代理（Proxy）', VPN: 'VPN', Tor: 'Tor', Server: '机房（Hosting）', Abuser: '滥用者', Robot: '机器人'};
const HTTPBL_KINDS = {suspicious: '可疑', harvester: '采集邮箱', spammer: '评论垃圾'};
const CHANGE = {purity: v => `${v}`, risk: v => `${v}`, factor: v => `${v} 家`, media: v => v || '无'};
const changeText = c => `${c.name} ${CHANGE[c.kind](c.from)} → ${CHANGE[c.kind](c.to)}`;
const shortDate = msValue => { const d = new Date(msValue); return `${d.getMonth() + 1}/${d.getDate()}`; };
const waitText = computed(() => ({queued: '排队中', preparing: '准备中', starting: '启动中', running: '检测中', collecting: '整理结果'})[pending.value?.state] || '检测中');
const RING = 2 * Math.PI * 15;
</script>
<template>
  <section ref="el" class="ix-card dt-section ipq" :class="{'is-waiting': !data && !error}" data-jump="ip" aria-label="IP 质量">
    <header class="ix-card-head"><h3><CardIcon :icon="DataAnalysis" />IP 质量</h3>
      <span v-if="report" class="instance-muted">{{ relativeTime(report.atMs, now) }}检测 · {{ report.family || 'IPv4' }}</span><span class="ob-grow" />
      <button v-if="online || pending" type="button" class="ix-text-button" :disabled="busy || starting || Boolean(pending)" @click="recheck">{{ pending ? `${waitText}…` : report ? '重新检测' : '检测' }}</button>
    </header>
    <p v-if="error" class="instance-error">{{ error }}</p>
    <p v-else-if="!data" class="instance-muted dt-wait" role="status">正在读取…</p>
    <template v-else>
      <p v-if="!report" class="dt-empty">{{ pending ? '正在运行 IP.Check.Place，大约 1～3 分钟。'
        : auto ? (online ? '还没有检测过，已在自动检测的队列里：轮到时会在这台服务器上运行 IP.Check.Place，同时最多 2 台、每台 1～3 分钟。想马上看可以点「检测」。'
          : '还没有检测过，这台上线后会自动检测。')
        : '还没有检测过。检测会在服务器上运行 IP.Check.Place，看 IP 类型、各家数据库的风险评分和平台解锁情况。' }}</p>

      <div v-else class="ipq-hero" :data-tone="purity?.tone">
        <div class="ipq-who">
          <strong class="mono">{{ ip || '—' }}</strong>
          <span><span v-if="flag(report.location.country)" class="sv-flag" aria-hidden="true">{{ flag(report.location.country) }}</span>{{ [place, report.org].filter(Boolean).join(' · ') || '位置未知' }}</span>
        </div>
        <div v-if="purity && purity.value != null" class="ipq-gauge" :title="PURITY_NOTE">
          <div class="ipq-scale" aria-hidden="true"><i :style="{left: `${purity.value}%`}" /></div>
          <div class="ipq-ticks" aria-hidden="true"><span>0 高危</span><span>25</span><span>50</span><span>75</span><span>100 可信</span></div>
        </div>
        <span v-else-if="purity" class="ipq-gauge instance-muted">各家风险库都没有给出结果</span>
        <div v-if="purity" class="ipq-score" :title="purity.value == null ? NO_PURITY_NOTE : PURITY_NOTE" aria-label="纯净分"><strong>{{ purity.value ?? '—' }}</strong><b>{{ purity.label }}</b></div>
      </div>
      <p v-if="report?.moved" class="dt-note ipq-moved" data-ip-moved>这份报告测的是之前的地址{{ report.ip ? `（${report.ip}）` : '' }}，服务器现在的地址已经变了，重新检测后才是当前地址的结果。</p>

      <div class="ipq-grid">
        <section v-if="report" class="ipq-panel" aria-label="使用场景 / 类型">
          <h4>使用场景 / 类型</h4>
          <dl>
            <div v-if="native"><dt>IP 原生性</dt><dd><span v-if="flag(native.country)" class="sv-flag" aria-hidden="true">{{ flag(native.country) }}</span>{{ native.name }}
              <b class="ipq-tag" :data-tone="native.native ? 'ok' : 'warn'">{{ native.text }}</b><small v-if="native.registered" class="ipq-sub">注册地 {{ native.registered }}</small></dd></div>
            <div v-if="usage"><dt>标记</dt><dd><b class="ipq-tag" :data-tone="usage.tone">{{ usage.text }}</b><small class="ipq-sub">{{ usage.n }}/{{ usage.total }} 家</small></dd></div>
            <div v-if="company"><dt>运营商类型</dt><dd><span class="ipq-em">{{ company.value }}</span><small class="ipq-sub">{{ company.n }}/{{ company.total }} 家</small></dd></div>
            <div><dt>人机流量</dt>
              <dd v-if="radar?.state === 'ok'" class="ipq-traffic" :title="'Cloudflare Radar：这个 ASN 最近 7 天的 HTTP 请求里，人和机器各占多少'">
                <span class="ipq-split" aria-hidden="true"><i :style="{width: `${radar.human}%`}" /><i :style="{width: `${radar.bot}%`}" /></span>
                <small><span class="is-human">人类 {{ radar.human }}%</span><span class="is-bot">机器 {{ radar.bot }}%</span></small>
                <b class="ipq-tag" :data-tone="radar.human >= radar.bot ? 'ok' : 'warn'">{{ radar.human >= radar.bot ? '人类偏多' : '机器偏多' }}</b></dd>
              <dd v-else class="instance-muted">{{ missing(radar) || (radar.state === 'unconfigured' ? '未配置（服务器设置 → 检测）' : 'Radar 没有这个 ASN 的数据') }}</dd></div>
            <div v-if="report.scenes?.length" class="ipq-scenes-row"><dt>场景评估<el-icon class="ipq-help" :title="SCENE_NOTE"><InfoFilled /></el-icon></dt>
              <dd><ul class="ipq-scenes">
                <li v-for="sc in report.scenes || []" :key="sc.key" :data-tone="SCENE_TONES[sc.label]" :title="sc.status ? `${sc.platform}：${sc.status}` : ''">
                  <svg viewBox="0 0 36 36" aria-hidden="true"><circle class="is-track" cx="18" cy="18" r="15" /><circle class="is-value" cx="18" cy="18" r="15"
                    :stroke-dasharray="`${RING * sc.value / 10} ${RING}`" transform="rotate(-90 18 18)" /><text x="18" y="22" text-anchor="middle">{{ sc.value }}</text></svg>
                  <span><b>{{ sc.name }}</b><small>{{ sc.label }}</small></span></li>
              </ul></dd></div>
            <div v-if="report.org"><dt>服务商</dt><dd>{{ report.org }}</dd></div>
          </dl>
        </section>

        <section class="ipq-panel" aria-label="ASN / 运营商">
          <h4>ASN / 运营商</h4>
          <dl>
            <div><dt>ASN</dt><dd v-if="asn" class="mono">AS{{ asn }}<button type="button" class="ix-icon-button ipq-copy" :aria-label="`复制 AS${asn}`" @click="copy(`AS${asn}`)"><el-icon><CopyDocument /></el-icon></button></dd>
              <dd v-else class="instance-muted">{{ missing(null) || '—' }}</dd></div>
            <div><dt>ASN 归属</dt><dd>{{ report?.org || net?.rdap?.name || missing(net?.rdap) || '—' }}</dd></div>
            <div><dt>ASN 自报类型</dt>
              <dd v-if="net?.peeringdb?.type"><span class="ipq-em">{{ net.peeringdb.type }}</span><small v-if="PEERING_TYPES[net.peeringdb.type]" class="ipq-sub">{{ PEERING_TYPES[net.peeringdb.type] }}</small></dd>
              <dd v-else class="instance-muted">{{ missing(net?.peeringdb) || '未在 PeeringDB 登记' }}</dd></div>
            <div><dt>ASN IPv4 总量</dt><dd v-if="net?.space">{{ net.space.ips.toLocaleString() }}<small class="ipq-sub">{{ net.space.prefixes }} 个前缀</small></dd>
              <dd v-else class="instance-muted">{{ missing(net?.space) }}</dd></div>
            <div><dt>预估带宽</dt><dd v-if="net?.peeringdb?.traffic">{{ net.peeringdb.traffic }}<small class="ipq-sub">PeeringDB 自报</small></dd>
              <dd v-else class="instance-muted">{{ missing(net?.peeringdb) || '未填写' }}</dd></div>
            <div><dt>ASN 注册日期</dt><dd v-if="net?.rdap?.registered">{{ net.rdap.registered }}</dd><dd v-else class="instance-muted">{{ missing(net?.rdap) || '—' }}</dd></div>
            <div><dt>企业信息</dt><dd v-if="net?.rdap?.org">{{ net.rdap.org }}</dd><dd v-else class="instance-muted">{{ missing(net?.rdap) || '—' }}</dd></div>
          </dl>
        </section>

        <section class="ipq-panel" :class="{'is-third': report}" aria-label="技术指标">
          <h4>技术指标</h4>
          <dl>
            <div><dt>Bogon</dt><dd v-if="intel?.ip"><b class="ipq-tag" :data-tone="intel.bogon ? 'warn' : 'ok'">{{ intel.bogon ? '是（不在公网路由）' : '否（公网可达）' }}</b></dd>
              <dd v-else class="instance-muted">{{ missing(null) || '没有公网 IPv4' }}</dd></div>
            <div><dt>反向 DNS</dt><dd v-if="intel?.rdns" class="mono ipq-wrap">{{ intel.rdns }}</dd><dd v-else class="instance-muted">{{ missing(intel?.rdns) || '没有' }}</dd></div>
            <div class="ipq-ports-row"><dt>开放端口</dt>
              <dd>
                <button type="button" class="ipq-tag is-action" :disabled="scanning || !intel?.ip" @click="scan">{{ scanning ? '探测中…' : ports ? '重新探测' : '检测常见端口' }}</button>
                <template v-if="ports">
                  <ul v-if="ports.open.length" class="ipq-ports"><li v-for="p in ports.open" :key="p.port" :data-tone="RISKY_PORTS.has(p.port) ? 'warn' : null">{{ p.port }} {{ p.name }}</li></ul>
                  <small class="ipq-sub">{{ ports.open.length ? '' : '常见端口都没开 · ' }}{{ relativeTime(ports.atMs, now) }}从{{ ports.fromSelf ? '本机自己' : '主控' }}探测 {{ ports.checked }} 个</small>
                </template>
              </dd></div>
            <div v-if="report?.location.timezone"><dt>时区</dt><dd>{{ report.location.timezone }}</dd></div>
            <div v-if="report"><dt>邮局 25 端口</dt><dd><b class="ipq-tag" :data-tone="report.mail.port25 ? 'ok' : null">{{ report.mail.port25 ? '可用' : '不通' }}</b></dd></div>
          </dl>
        </section>

        <section v-if="report" class="ipq-panel is-third" aria-label="IP 情报（威胁指标）">
          <h4>IP 情报（威胁指标）</h4>
          <dl>
            <div><dt>风险标记</dt><dd><b class="ipq-tag" :data-tone="threats.length ? 'bad' : voted ? 'ok' : 'off'">{{ threats.length ? threats.map(f => f.name).join('、') : voted ? '未发现明显威胁' : '没有数据' }}</b></dd></div>
            <div><dt>滥用等级</dt><dd v-if="abuse"><b class="ipq-tag" :data-tone="abuse.tone">{{ abuse.text }}</b><small class="ipq-sub">AbuseIPDB</small></dd><dd v-else class="instance-muted">—</dd></div>
            <div><dt>综合风险</dt><dd><b class="ipq-tag" :data-tone="level(report.risk.level)[0]">{{ report.risk.value == null ? '—' : `${Math.round(report.risk.value)} · ${level(report.risk.level)[1]}` }}</b>
              <small class="ipq-sub">{{ report.risk.sources ? `${report.risk.sources} 家中最高` : '没有评分' }}</small></dd></div>
            <div><dt>HTTP 蜜罐黑名单</dt>
              <dd v-if="intel?.httpbl?.state === 'ok'"><b class="ipq-tag" :data-tone="intel.httpbl.listed ? 'bad' : 'ok'">{{ intel.httpbl.listed ? `上榜 · 威胁 ${intel.httpbl.threat}` : '纯净' }}</b>
                <small v-if="intel.httpbl.listed" class="ipq-sub">{{ intel.httpbl.days }} 天前活动{{ intel.httpbl.kinds.length ? ` · ${intel.httpbl.kinds.map(k => HTTPBL_KINDS[k]).join('、')}` : '' }}</small></dd>
              <dd v-else class="instance-muted">{{ missing(intel?.httpbl) || '未配置（服务器设置 → 检测）' }}</dd></div>
            <div><dt>邮件黑名单</dt><dd v-if="!report.mail.checked"><b class="ipq-tag" data-tone="off">没有数据</b></dd>
              <dd v-else><b class="ipq-tag" :data-tone="report.mail.listed ? 'bad' : 'ok'">{{ report.mail.listed ? `上榜 ${report.mail.listed} 个` : '未上榜' }}</b>
              <small class="ipq-sub">共查 {{ report.mail.checked }} 个{{ report.mail.marked ? ` · ${report.mail.marked} 个标记` : '' }}</small></dd></div>
          </dl>
        </section>

        <section v-if="report" class="ipq-panel is-third" aria-label="风险深度检测">
          <h4>风险深度检测</h4>
          <dl>
            <div v-for="f in report.factors" :key="f.key" :title="f.by.length ? `判定的数据库：${f.by.join('、')}` : '没有数据库判定'"><dt>{{ FACTOR_NAMES[f.key] || f.name }}</dt>
              <dd><b class="ipq-tag" :data-tone="f.flagged ? (f.flagged * 2 > f.total ? 'bad' : 'warn') : f.total ? 'ok' : 'off'">{{ f.flagged ? `${f.flagged}/${f.total} 家判定` : f.total ? '未检测到' : '没有数据' }}</b>
                <small v-if="!f.flagged && f.total" class="ipq-sub">{{ f.total }} 家</small></dd></div>
          </dl>
        </section>

        <section v-if="report" class="ipq-panel ipq-scores-panel" aria-label="各家评分">
          <h4>各家风险评分</h4>
          <ul class="ipq-scores" aria-label="各家评分">
            <li v-for="sc in scores" :key="sc.source" :data-tone="sc.value == null ? 'off' : sc.tone">
              <span>{{ sc.name }}</span><i class="ipq-bar" aria-hidden="true"><b :style="{width: `${Math.max(sc.value ?? 0, sc.value ? 2 : 0)}%`}" /></i><strong>{{ sc.value == null ? '—' : Math.round(sc.value) }}</strong>
            </li>
          </ul>
        </section>
        <figure v-if="report" class="ipq-map">
          <IpMap v-if="seen" :key="JSON.stringify([report.location.lat, report.location.lon, report.location.country])"
            :lat="report.location.lat" :lon="report.location.lon" :country="report.location.country" />
          <figcaption><span v-if="report.kind" class="pb-tag ipq-kind" :data-tone="report.kind.includes('原生') ? 'ok' : null">{{ report.kind }}</span>{{ place || '位置未知' }}
            <small v-if="report.location.country && (report.location.lat == null || report.location.lon == null)" class="instance-muted">按国家定位</small></figcaption>
        </figure>
      </div>

      <ul v-if="!unlockShown && report?.media?.length" class="ipq-media" aria-label="平台解锁">
        <li v-for="m in report.media" :key="m.key" :data-state="m.status === '解锁' ? 'ok' : m.status === '屏蔽' || m.status === '失败' ? 'blocked' : 'limited'">
          <span>{{ m.name }}</span><b>{{ [m.status, m.region].filter(Boolean).join(' ') }}</b></li>
      </ul>
      <p v-if="data.changes?.length" class="ipq-changes" data-ip-changes><span>与上次（{{ shortDate(data.previousAtMs) }}）相比</span>{{ data.changes.map(changeText).join('；') }}</p>
    </template>
  </section>
</template>
