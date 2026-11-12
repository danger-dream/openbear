<script setup>
import { computed, ref } from 'vue';
import { ElMessage } from 'element-plus';
import { copyTextToClipboard } from '../../utils/clipboard.js';
import { bytes, noAutofill } from '../serverHelpers.js';
import { mesh, save } from './meshStore.js';
import { errorsAt, LINK_SCHEMES, portTarget, proxyName, proxyPorts, proxyRun, proxyUrl, setExit as setMemberExit, uid } from './meshHelpers.js';
import MeshFr from './MeshFr.vue';
import MeshPortEdit from './MeshPortEdit.vue';

// A node's 出口和代理端口: as an exit it is a SOCKS5 / HTTP proxy on its mesh address that every member can use; its proxy
// ports are ports on it leading to an exit or an exit group through the tunnel.
const props = defineProps({member: {type: Object, required: true}, index: {type: Number, required: true}, phone: Boolean});
const emit = defineEmits(['group']);
const m = computed(() => props.member);
const id = computed(() => m.value.serverId);
const at = f => errorsAt(mesh.errors, `members[${props.index}].proxy.${f}`);
const linux = computed(() => (m.value.platform || 'linux') === 'linux');
const exit = computed(() => m.value.proxy?.exit || null);
const groups = computed(() => mesh.work?.exitGroups || []);
const myGroups = computed(() => groups.value.filter(g => g.members.includes(id.value)));
const run = computed(() => mesh.data?.nodes?.[id.value]?.summary?.proxy || null);
const exitEntry = computed(() => proxyRun(mesh.data?.nodes?.[id.value]?.summary, 'exit'));

function setExit(on) { setMemberExit(mesh.work, m.value, on); void save(); }
function setExitField(key, value) { exit.value[key] = value; void save(); }
function setUser(value) {
  const v = String(value || '').trim().toLowerCase();
  setExitField('user', v || proxyName(m.value, mesh.work.members));
}
function setExitPort(value) { const n = Number(value); if (Number.isInteger(n)) setExitField('port', n); }
function joinGroup(gid) {
  if (!gid) return;
  if (gid === 'new') {
    const g = {id: uid('g'), name: m.value.name, members: [id.value]};
    mesh.work.exitGroups = [...groups.value, g];
    void save();
    emit('group', g.id);
    return;
  }
  const g = groups.value.find(x => x.id === gid);
  if (g) g.members = [...g.members, id.value];
  void save();
}
function leaveGroup(g) { g.members = g.members.filter(x => x !== id.value); void save(); }

const scheme = ref('socks5');
const shown = ref(false);
const ready = computed(() => Boolean(exit.value && m.value.address && exit.value.password));
const link = (masked = false) => proxyUrl(scheme.value, exit.value.user, exit.value.password, m.value.address, exit.value.port, {masked});
async function copy(text, what) {
  try { await copyTextToClipboard(text); ElMessage.success(`已复制${what}`); } catch { ElMessage.error('复制失败'); }
}

const ports = computed(() => proxyPorts(m.value));
const port = ref(null);
const portErrors = i => errorsAt(mesh.errors, `members[${props.index}].proxy.ports[${i}]`).length
  || (mesh.errors || []).some(e => e.path?.startsWith(`members[${props.index}].proxy.ports[${i}].`));
</script>
<template>
  <section id="m3-sec-proxy" class="m3-dsec" aria-label="出口和代理端口">
    <h3>出口和代理端口</h3>
    <p class="m3-explain">当<b>出口</b>时，这台在自己的组网地址上提供 SOCKS5 / HTTP 代理，组网里的程序复制链接就能用，对外显示这台的 IP。
      <b>代理端口</b>开在程序所在的机器上，经提前建好的隧道走某个出口或出口组，新连接少 2～3 个跨组网往返。</p>
    <h4 class="m3-subh">出口</h4>
    <MeshFr label="当出口" :errors="at('exit')">
      <button type="button" class="m3-sw" role="switch" :aria-checked="Boolean(exit)" aria-label="当出口" @click="setExit(!exit)"><i></i><span>{{ exit ? '已打开' : '关闭' }}</span></button>
    </MeshFr>
    <template v-if="exit">
      <MeshFr label="链接" hint="组网里的程序填这个代理；socks5h:// 把域名交给出口解析。改动应用后生效。">
        <div class="m3-ctl"><div class="m3-seg" role="radiogroup" aria-label="链接格式"><button v-for="[k, l] in LINK_SCHEMES" :key="k" type="button" role="radio" :aria-checked="scheme === k" @click="scheme = k">{{ l }}</button></div></div>
        <div class="m3-linkline"><span class="mono m3-faint m3-ellipsis">{{ ready ? link(true) : '保存后生成密码' }}</span>
          <el-button size="small" :disabled="!ready" @click="copy(link(), ` ${m.name} 的出口`)">复制</el-button></div>
      </MeshFr>
      <MeshFr label="用户名" :errors="at('exit.user')">
        <input v-bind="noAutofill" class="m3-input m3-mid mono" maxlength="32" :value="exit.user" aria-label="出口用户名" @change="setUser($event.target.value)" />
      </MeshFr>
      <MeshFr label="密码" :errors="at('exit.password')" hint="重新生成后，用着这个出口的程序都要换密码。">
        <div class="m3-ctl"><span class="mono">{{ exit.password ? (shown ? exit.password : '••••••••') : '保存后自动生成' }}</span>
          <template v-if="exit.password"><button type="button" class="ix-text-button" @click="shown = !shown">{{ shown ? '隐藏' : '显示' }}</button>
            <button type="button" class="ix-text-button" @click="copy(exit.password, '密码')">复制</button>
            <button type="button" class="ix-text-button" @click="setExitField('password', '')">重新生成</button></template></div>
      </MeshFr>
      <MeshFr label="端口" :auto="{value: '1081', why: '默认 1081'}" :changed="exit.port !== 1081" :errors="at('exit.port')" hint="SOCKS5 和 HTTP 共用，只在组网地址上监听。">
        <input v-bind="noAutofill" class="m3-input m3-short mono" type="number" min="1" max="65535" :value="exit.port" aria-label="出口端口" @change="setExitPort($event.target.value)" />
      </MeshFr>
      <MeshFr label="谁能用" :auto="{value: '组网成员', why: '所有组网成员凭用户名和密码使用'}" :changed="Boolean(exit.subnets)" restorable @restore="setExitField('subnets', false)"
        hint="共享局域网指各网关宣告的网段，比如家里的电脑、手机经软路由连进组网。">
        <button type="button" class="m3-sw" role="switch" :aria-checked="Boolean(exit.subnets)" aria-label="也允许共享局域网里的设备" @click="setExitField('subnets', !exit.subnets)"><i></i><span>也允许共享局域网里的设备</span></button>
      </MeshFr>
      <MeshFr label="所在出口组">
        <div class="m3-ctl">
          <span v-for="g in myGroups" :key="g.id" class="m3-chip-btn"><button type="button" class="ix-text-button" @click="emit('group', g.id)">{{ g.name }}</button>
            <button type="button" class="ix-icon-button" :aria-label="`移出出口组 ${g.name}`" title="移出" @click="leaveGroup(g)">✕</button></span>
          <span v-if="!myGroups.length" class="m3-faint">不在任何出口组里</span>
          <select class="m3-sel" value="" aria-label="加到出口组" @change="joinGroup($event.target.value); $event.target.value = ''">
            <option value="">＋ 加到出口组…</option>
            <option v-for="g in groups.filter(x => !x.members.includes(id))" :key="g.id" :value="g.id">{{ g.name }}</option>
            <option value="new">新建出口组「{{ m.name }}」</option>
          </select>
        </div>
      </MeshFr>
      <MeshFr v-if="run?.exit" label="运行情况">
        <p v-if="run.exit.error" class="ob-bad">出口没有启动：{{ run.exit.error }}</p>
        <small v-for="e in exitEntry?.errors || []" :key="e" class="ob-bad m3-block">代理没有监听上：{{ e }}</small>
        <span v-if="!run.exit.error" class="m3-faint">当前 {{ run.exit.active }} 个连接 · 累计 {{ run.exit.opened }} 个<template v-if="run.exit.failed || run.exit.refused">（失败 {{ run.exit.failed }} · 拒绝 {{ run.exit.refused }}）</template> · ↓ {{ bytes(run.exit.bytesIn) }} · ↑ {{ bytes(run.exit.bytesOut) }}<template v-if="exitEntry?.rejected"> · 密码错误 {{ exitEntry.rejected }} 次</template></span>
      </MeshFr>
      <MeshFr label="访问内网" :auto="{value: '不允许', why: '防止经出口访问对方机房的内网'}" :changed="Boolean(exit.lan)" restorable @restore="setExitField('lan', false)"
        hint="内网指私有地址、100.64.0.0/10、回环和这个节点自己的地址；组网地址段始终不行。">
        <button type="button" class="m3-sw" role="switch" :aria-checked="Boolean(exit.lan)" aria-label="允许访问内网" @click="setExitField('lan', !exit.lan)"><i></i><span>允许经它访问它所在的内网地址</span></button>
      </MeshFr>
      <MeshFr label="出站网卡" :auto="{value: '系统默认路由', why: '按系统路由选网卡'}" :changed="Boolean(exit.interface)" :errors="at('exit.interface')"
        hint="多网卡或这个节点自己的流量经过代理、TUN（比如 OpenClash 的 TUN 模式）时才需要：指定从哪块网卡出去。">
        <input v-bind="noAutofill" class="m3-input m3-mid mono" maxlength="15" :value="exit.interface || ''" placeholder="系统默认" :disabled="!linux" aria-label="出站网卡" @change="setExitField('interface', $event.target.value.trim())" />
      </MeshFr>
      <MeshFr label="源地址" :auto="{value: '系统默认', why: '按系统路由选地址'}" :changed="Boolean(exit.source)" :errors="at('exit.source')" hint="有多个公网 IP 时，指定用哪个 IP 出去。">
        <input v-bind="noAutofill" class="m3-input m3-mid mono" maxlength="45" :value="exit.source || ''" placeholder="系统默认" aria-label="出站源地址" @change="setExitField('source', $event.target.value.trim())" />
      </MeshFr>
    </template>
    <h4 class="m3-subh">代理端口</h4>
    <MeshFr label="代理端口" :errors="at('ports')" hint="这台上的程序（或开放后局域网里的设备）连这里，就从选的出口或出口组出去。比如路由器上开 1080，路由器上的 Clash 连 127.0.0.1:1080。">
      <button v-for="(p, i) in ports" :key="p.id" type="button" class="m3-rel" @click="port = {member: id, id: p.id}">
        <span class="ob-grow"><b class="mono">:{{ p.port }}</b> · {{ p.listen === 'lan' && p.address ? `局域网 ${p.address}` : '仅本机' }} · 走 {{ portTarget(mesh.work, p) || '还没选' }}
          <span v-if="portErrors(i)" class="m3-tag" data-kind="bad">需修改</span></span><span class="m3-chev">›</span></button>
      <div class="m3-ctl"><el-button size="small" @click="port = {member: id, create: true}">＋ 新建代理端口</el-button></div>
    </MeshFr>
    <MeshPortEdit :at="port" :phone="phone" @close="port = null" />
  </section>
</template>
