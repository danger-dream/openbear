<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { Key } from '@element-plus/icons-vue';
import { Api } from '../../api.js';
import { ok } from '../../instances/instanceHelpers.js';
import { canDesktop as checkDesktop, hasDesktop as checkHasDesktop, noAutofill, platformOf, safeError } from '../serverHelpers.js';
import CardIcon from './CardIcon.vue';

// 连接属性: SSH login and (for Windows / macOS) RDP / VNC desktop credentials and port.
const props = defineProps({server: {type: Object, required: true}, busy: Boolean, active: {type: Boolean, default: true}});
const emit = defineEmits(['check', 'edit', 'desktop']);

const platform = computed(() => platformOf(props.server));
const isMac = computed(() => platform.value === 'darwin');
const showDesktop = computed(() => checkHasDesktop(props.server));
const canOpenDesktop = computed(() => checkDesktop(props.server));

const desktopCfg = ref(null);
const dialogOpen = ref(false), saving = ref(false);
const form = reactive({port: '', defaultPort: 3389, username: '', password: '', domain: '', hasSaved: false});

async function loadDesktopConfig() {
  if (!showDesktop.value || !props.server?.id) {
    desktopCfg.value = null;
    return;
  }
  try {
    const cfg = ok(await Api.serverDesktopConfig(props.server.id));
    desktopCfg.value = cfg;
  } catch { /* ignore */ }
}

function openDesktopDialog() {
  const cfg = desktopCfg.value || {};
  form.defaultPort = cfg.defaultPort || (isMac.value ? 5900 : 3389);
  form.port = cfg.customPort ? String(cfg.customPort) : '';
  form.username = cfg.username || props.server.ssh?.username || (isMac.value ? '' : 'Administrator');
  form.password = cfg.password || '';
  form.domain = cfg.domain || '';
  form.hasSaved = Boolean(cfg.hasSaved);
  dialogOpen.value = true;
}

async function saveDesktopConfig() {
  if (saving.value) return;
  saving.value = true;
  try {
    desktopCfg.value = ok(await Api.saveServerDesktopConfig(props.server.id, {
      port: form.port ? Number(form.port) : 0,
      username: form.username,
      password: form.password,
      domain: form.domain,
    }));
    dialogOpen.value = false;
    ElMessage.success('桌面连接属性已保存');
  } catch (e) {
    ElMessage.error(safeError(e));
  } finally {
    saving.value = false;
  }
}

async function clearDesktopConfig() {
  if (saving.value) return;
  saving.value = true;
  try {
    desktopCfg.value = ok(await Api.saveServerDesktopConfig(props.server.id, {clear: true}));
    dialogOpen.value = false;
    ElMessage.success('已恢复跟随 SSH 凭据与默认端口');
  } catch (e) {
    ElMessage.error(safeError(e));
  } finally {
    saving.value = false;
  }
}

const desktopSummary = computed(() => {
  const cfg = desktopCfg.value;
  const defPort = isMac.value ? 5900 : 3389;
  const proto = isMac.value ? 'VNC (noVNC)' : 'RDP (IronRDP)';
  if (!cfg) {
    return {
      proto,
      port: `127.0.0.1:${defPort}`,
      account: props.server.ssh?.username || (isMac.value ? '未设置' : 'Administrator'),
      auth: props.server.ssh?.auth === 'password' ? '跟随 SSH 密码' : '连接时输入密码',
    };
  }
  return {
    proto,
    port: `127.0.0.1:${cfg.port || defPort}${cfg.customPort ? '（自定义）' : '（默认）'}`,
    account: [cfg.domain, cfg.username].filter(Boolean).join('\\') || (isMac.value ? '纯密码认证' : 'Administrator'),
    auth: cfg.source === 'desktop' ? '独立桌面凭据' : cfg.source === 'ssh' ? '跟随 SSH 凭据' : '未保存密码',
  };
});

watch(() => [props.server?.id, props.server?.ssh?.username, props.server?.ssh?.auth, showDesktop.value], () => {
  void loadDesktopConfig();
});
onMounted(() => { void loadDesktopConfig(); });
</script>
<template>
  <section class="ix-card sv-conn-card" data-part="ssh">
    <header class="ix-card-head">
      <h3><CardIcon :icon="Key" tone="violet" />连接属性</h3>
      <span class="ob-grow" />
      <template v-if="!showDesktop && server.ssh">
        <button type="button" class="ix-text-button" :disabled="busy" @click="emit('check')">测试</button>
        <button type="button" class="ix-text-button" @click="emit('edit')">编辑</button>
      </template>
    </header>

    <!-- SSH section -->
    <div v-if="server.ssh || !showDesktop" class="sv-conn-block">
      <div v-if="showDesktop" class="sv-conn-subhead">
        <strong>SSH 终端连接</strong>
        <span class="ob-grow" />
        <button v-if="server.ssh" type="button" class="ix-text-button" :disabled="busy" @click="emit('check')">测试</button>
        <button type="button" class="ix-text-button" @click="emit('edit')">{{ server.ssh ? '编辑' : '登记' }}</button>
      </div>
      <dl v-if="server.ssh" class="ix-facts">
        <div><dt>地址</dt><dd class="mono ix-ellipsis">{{ server.ssh.username }}@{{ server.ssh.host }}:{{ server.ssh.port }}</dd></div>
        <div><dt>认证</dt><dd class="ix-ellipsis">{{ server.ssh.keyLabel || '密码' }}</dd></div>
        <div><dt>状态</dt><dd><span class="ob-dot" :data-tone="server.ssh.status === 'ok' ? 'ok' : server.ssh.status === 'unreachable' ? 'bad' : 'off'">{{ {ok: '连接正常', unreachable: '无法连接'}[server.ssh.status] || '尚未验证' }}</span></dd></div>
        <div v-if="server.usedBy.length"><dt>使用中</dt><dd>{{ server.usedBy.join('、') }}</dd></div>
      </dl>
    </div>
    <div v-else-if="!server.local" class="sv-conn-block">
      <div class="sv-conn-subhead">
        <strong>SSH 终端连接</strong>
        <span class="ob-grow" />
        <button type="button" class="ix-text-button" @click="emit('edit')">登记 SSH</button>
      </div>
      <p class="instance-muted sv-conn-empty">尚未登记 SSH 连接信息。</p>
    </div>

    <!-- RDP / VNC Desktop section (Windows & macOS) -->
    <div v-if="showDesktop" class="sv-conn-block is-desktop" data-desktop-props>
      <div class="sv-conn-subhead">
        <strong>{{ isMac ? 'VNC 屏幕共享' : 'RDP 远程桌面' }}</strong>
        <span class="ob-grow" />
        <button v-if="canOpenDesktop" type="button" class="ix-text-button" @click="emit('desktop')">查看桌面</button>
        <button type="button" class="ix-text-button" @click="openDesktopDialog">设置</button>
      </div>
      <dl class="ix-facts">
        <div><dt>协议</dt><dd class="ix-ellipsis">{{ desktopSummary.proto }}</dd></div>
        <div><dt>转发端口</dt><dd class="mono ix-ellipsis">{{ desktopSummary.port }}</dd></div>
        <div><dt>登录账号</dt><dd class="mono ix-ellipsis">{{ desktopSummary.account }}</dd></div>
        <div><dt>凭据来源</dt><dd class="ix-ellipsis">{{ desktopSummary.auth }}</dd></div>
      </dl>
    </div>

    <el-dialog v-model="dialogOpen" :title="`${server.name} · ${isMac ? 'VNC 屏幕共享' : 'RDP 远程桌面'}属性`" width="440px" class="admin-dialog instance-dialog instance-ui servers-ui" append-to-body>
      <form class="instance-form" autocomplete="off" @submit.prevent="saveDesktopConfig">
        <div class="dp-fields">
          <label class="dp-field">
            <span>登录用户名</span>
            <input v-model="form.username" v-bind="noAutofill" type="text" :placeholder="isMac ? 'macOS 系统用户名（纯 VNC 密码可留空）' : '例如 Administrator'" />
          </label>
          <label class="dp-field">
            <span>登录密码</span>
            <input v-model="form.password" v-bind="noAutofill" type="password" :placeholder="form.hasSaved ? '已保存密码，不修改可留空' : '输入远程桌面或系统登录密码'" />
          </label>
          <div class="dp-field-row">
            <label class="dp-field">
              <span>本机端口</span>
              <input v-model="form.port" v-bind="noAutofill" type="number" min="1" max="65535" class="mono" :placeholder="`默认 ${form.defaultPort}`" />
            </label>
            <label v-if="!isMac" class="dp-field">
              <span>域（可选）</span>
              <input v-model="form.domain" v-bind="noAutofill" type="text" placeholder="留空为本地账户" />
            </label>
          </div>
        </div>
        <small class="instance-muted">经节点的反向连接转发到本机远程桌面端口；密码加密保存在主控数据库中。不单独设置时，默认沿用这台服务器的 SSH 用户名和密码。</small>
        <div class="instance-form-footer">
          <el-button v-if="form.hasSaved" type="danger" plain :loading="saving" @click="clearDesktopConfig">恢复跟随 SSH</el-button>
          <span class="ob-grow" />
          <el-button @click="dialogOpen = false">取消</el-button>
          <el-button type="primary" native-type="submit" :loading="saving">保存</el-button>
        </div>
      </form>
    </el-dialog>
  </section>
</template>
