<script setup>
import { onBeforeUnmount, onMounted, reactive, ref, toRefs } from "vue";
import { authMode } from '../authMode.js';
import { pageSession, announceSessionChange, endPageSession } from '../sessionBoundary.js';
import { retireGatewayPush } from '../pwa/pushClient.js';
import { ElMessage } from "element-plus";
import { Api } from "../api";
import { createLoginFlow } from "./loginFlow.js";
import { loginSuccessTarget } from "../loginRedirect.js";
import BearLogo from "../components/BearLogo.vue";

const state = reactive({secret: "", loading: false, requestUuid: "", status: "", retryAfter: 0, notice: ""});
const { secret, loading, requestUuid, status, retryAfter, notice } = toRefs(state);
const flow = createLoginFlow({
  state,
  api: Api,
  success: (message) => ElMessage.success(message),
  error: (message) => ElMessage.error(message),
  authenticated: () => window.location.replace(loginSuccessTarget(window.location)),
});
const username = ref('');
const password = ref('');
const passwordBusy = ref(false);
const passwordError = ref('');
const logoutRequired = ref(false);
async function logoutGateway() {
  if (passwordBusy.value) return;
  passwordBusy.value = true;
  try {
    if (!pageSession.contextToken) { window.location.assign('/login'); return; }
    await retireGatewayPush();
    const result = await Api.logout();
    if (result?.ok !== true) throw new Error('logout_failed');
    endPageSession({broadcast: true}); window.location.replace('/login');
  } catch { passwordError.value = '退出未确认成功，请检查连接后重试。'; }
  finally { passwordBusy.value = false; }
}
let alive = true;
async function submitPassword() {
  if (!['managed', 'gateway'].includes(authMode.value) || passwordBusy.value || !username.value.trim() || !password.value) return;
  passwordBusy.value = true;
  passwordError.value = '';
  try {
    const result = await Api.passwordLogin(username.value.trim(), password.value);
    if (result?.ok !== true) throw new Error('login_failed');
    if (alive) { announceSessionChange(); window.location.replace(loginSuccessTarget(window.location)); }
  } catch (error) {
    if (alive) {
      logoutRequired.value = error?.response?.status === 409 && error?.response?.data?.error === 'logout_required';
      passwordError.value = logoutRequired.value ? '此浏览器已有登录。请先退出当前账号，再切换账号。' : error?.response?.status === 429 ? '尝试过于频繁，请稍后再试。' : '登录失败，请检查账号密码或网络连接。';
    }
  } finally { password.value = ''; passwordBusy.value = false; }
}
const submit = () => authMode.value === 'controller' && flow.submit();
onBeforeUnmount(() => { alive = false; password.value = ''; state.secret = ''; flow.dispose(); });
</script>

<template>
  <div class="login-screen min-h-screen flex items-center justify-center bg-macbg px-4 text-mactext">
    <section class="w-full max-w-md mac-panel mac-shadow p-7 space-y-5">
      <div class="text-center space-y-2">
        <div class="mx-auto w-14 h-14"><BearLogo /></div>
        <h1 class="text-xl font-semibold">OpenBear 管理台登录</h1>
        <p v-if="authMode === 'controller'" class="text-sm text-macsub">输入 Web Secret Key 后，需要在 Telegram 中二次确认。</p>
        <p v-else-if="authMode === 'managed' || authMode === 'gateway'" class="text-sm text-macsub">{{ authMode === 'gateway' ? '使用你的账号登录，自动进入你的实例。' : '使用此实例的账号密码登录。' }}</p>
      </div>

      <div v-if="authMode === 'gateway' && pageSession.contextToken" class="space-y-4"><p>当前已登录：{{ pageSession.username }}。切换账号前请先退出。</p><a href="/" class="mac-text-button">进入当前实例</a><el-button :loading="passwordBusy" @click="logoutGateway">退出当前账号</el-button><p v-if="passwordError" role="alert">{{ passwordError }}</p></div>
      <form v-else-if="authMode === 'managed' || authMode === 'gateway'" class="password-login space-y-4" @submit.prevent="submitPassword">
        <label>账号<el-input v-model="username" aria-label="账号" autocomplete="username" :disabled="passwordBusy" /></label>
        <label>密码<el-input v-model="password" aria-label="密码" type="password" autocomplete="current-password" :disabled="passwordBusy" /></label>
        <p v-if="passwordError" role="alert" class="text-ob-danger text-sm">{{ passwordError }}</p>
        <el-button v-if="logoutRequired" @click="logoutGateway">重新打开登录页并确认退出</el-button>
        <el-button type="primary" size="large" class="w-full" native-type="submit" :loading="passwordBusy" :disabled="passwordBusy || !username.trim() || !password">登录</el-button>
      </form>
      <el-form v-else @submit.prevent="submit" class="space-y-4">
        <el-input
          v-model="secret"
          type="password"
          autocomplete="current-password"
          placeholder="Web Secret Key"
          size="large"
          show-password
          :disabled="loading"
        />
        <el-button type="primary" size="large" class="w-full" :loading="loading" :disabled="loading || !secret" native-type="submit">
          {{ status === 'verifying' ? '正在确认登录…' : status === 'pending' ? '等待 Telegram 确认…' : '继续' }}
        </el-button>
      </el-form>

      <div v-if="requestUuid" class="rounded-2xl bg-ob-surface/70 border border-macborder p-4 text-sm space-y-2">
        <div class="font-medium">📲 {{ status === 'verifying' ? '正在确认登录会话' : '等待 Telegram 确认' }}</div>
        <div class="text-macsub break-all">请求：<span class="font-mono">{{ requestUuid }}</span></div>
        <div v-if="notice" class="text-macsub" role="status" aria-live="polite">{{ notice }}</div>
      </div>
      <div v-if="retryAfter" class="rounded-2xl bg-[var(--ob-danger-soft)] text-ob-danger border border-ob-danger/25 p-4 text-sm">
        登录请求过于频繁，服务要求等待 {{ retryAfter }} 秒后再试。
      </div>
    </section>
  </div>
</template>

<style scoped>
.password-login label { display: grid; gap: 6px; font-size: 13px; }
/* Keep desktop sizing intact; a short mobile/keyboard viewport can scroll naturally. */
@media (max-width: 767px) {
  .login-screen :deep(input) { font-size: 16px; }
  .login-screen {
    min-height: 100dvh;
    padding-top: max(1rem, env(safe-area-inset-top, 0px));
    padding-bottom: max(1rem, env(safe-area-inset-bottom, 0px));
    padding-left: max(1rem, env(safe-area-inset-left, 0px));
    padding-right: max(1rem, env(safe-area-inset-right, 0px));
  }
}
</style>
