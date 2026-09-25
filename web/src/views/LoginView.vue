<script setup>
import { onBeforeUnmount, reactive, toRefs } from "vue";
import { ElMessage } from "element-plus";
import { Api } from "../api";
import { createLoginFlow } from "./loginFlow.js";
import BearLogo from "../components/BearLogo.vue";

const state = reactive({secret: "", loading: false, requestUuid: "", status: "", retryAfter: 0, notice: ""});
const { secret, loading, requestUuid, status, retryAfter, notice } = toRefs(state);
const flow = createLoginFlow({
  state,
  api: Api,
  success: (message) => ElMessage.success(message),
  error: (message) => ElMessage.error(message),
  authenticated: () => window.location.replace("/"),
});
const submit = () => flow.submit();
onBeforeUnmount(() => flow.dispose());
</script>

<template>
  <div class="login-screen min-h-screen flex items-center justify-center bg-macbg px-4 text-mactext">
    <section class="w-full max-w-md mac-panel mac-shadow p-7 space-y-5">
      <div class="text-center space-y-2">
        <div class="mx-auto w-14 h-14 rounded-3xl bg-ob-surface border border-macborder shadow-sm flex items-center justify-center"><BearLogo /></div>
        <h1 class="text-xl font-semibold">OpenBear 管理台登录</h1>
        <p class="text-sm text-macsub">输入 Web Secret Key 后，需要在 Telegram 中二次确认。</p>
      </div>

      <el-form @submit.prevent="submit" class="space-y-4">
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
/* Keep desktop sizing intact; a short mobile/keyboard viewport can scroll naturally. */
@media (max-width: 767px) {
  .login-screen {
    min-height: 100dvh;
    padding-top: max(1rem, env(safe-area-inset-top, 0px));
    padding-bottom: max(1rem, env(safe-area-inset-bottom, 0px));
    padding-left: max(1rem, env(safe-area-inset-left, 0px));
    padding-right: max(1rem, env(safe-area-inset-right, 0px));
  }
}
</style>
