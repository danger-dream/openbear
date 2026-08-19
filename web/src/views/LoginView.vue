<script setup>
import { onBeforeUnmount, ref } from "vue";
import { ElMessage } from "element-plus";
import { Api, apiError } from "../api";

const secret = ref("");
const loading = ref(false);
const requestUuid = ref("");
const status = ref("");
const retryAfter = ref(0);
let timer = 0;

function clearPoll() {
  if (timer) {
    window.clearTimeout(timer);
    timer = 0;
  }
}

async function pollStatus() {
  if (!requestUuid.value) return;
  try {
    const result = await Api.loginStatus(requestUuid.value);
    status.value = result.status;
    if (result.status === "approved") {
      await Api.consumeLogin(requestUuid.value);
      window.location.href = "/";
      return;
    }
    if (result.status === "rejected" || result.status === "denied") {
      ElMessage.error("Telegram 已拒绝本次登录");
      loading.value = false;
      return;
    }
    if (result.status === "expired") {
      ElMessage.error("登录请求已过期，请重新输入 Secret Key");
      loading.value = false;
      return;
    }
    timer = window.setTimeout(pollStatus, 1800);
  } catch (error) {
    ElMessage.error(apiError(error));
    loading.value = false;
  }
}

async function submit() {
  clearPoll();
  retryAfter.value = 0;
  requestUuid.value = "";
  status.value = "";
  loading.value = true;
  try {
    const result = await Api.loginStart(secret.value);
    requestUuid.value = result.requestUuid;
    status.value = "pending";
    ElMessage.success("Secret Key 已通过，请在 Telegram 中确认登录");
    timer = window.setTimeout(pollStatus, 800);
  } catch (error) {
    retryAfter.value = Number(error?.response?.data?.retryAfter || 0);
    ElMessage.error(apiError(error));
    loading.value = false;
  }
}

onBeforeUnmount(clearPoll);
</script>

<template>
  <div class="min-h-screen flex items-center justify-center bg-macbg px-4 text-mactext">
    <section class="w-full max-w-md mac-panel mac-shadow p-7 space-y-5">
      <div class="text-center space-y-2">
        <div class="mx-auto w-14 h-14 rounded-3xl bg-white border border-macborder shadow-sm flex items-center justify-center text-3xl">🐻</div>
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
          :disabled="loading && status === 'pending'"
          @keyup.enter="submit"
        />
        <el-button type="primary" size="large" class="w-full" :loading="loading" :disabled="!secret" @click="submit">
          {{ status === 'pending' ? '等待 Telegram 确认…' : '继续' }}
        </el-button>
      </el-form>

      <div v-if="requestUuid" class="rounded-2xl bg-white/70 border border-macborder p-4 text-sm space-y-2">
        <div class="font-medium">📲 等待 Telegram 确认</div>
        <div class="text-macsub break-all">请求：<span class="font-mono">{{ requestUuid }}</span></div>
      </div>
      <div v-if="retryAfter" class="rounded-2xl bg-red-50 text-red-700 border border-red-100 p-4 text-sm">
        登录失败次数过多，请等待 {{ retryAfter }} 秒后再试。
      </div>
    </section>
  </div>
</template>
