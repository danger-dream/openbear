<script setup>
import { computed, onBeforeUnmount, ref, shallowRef } from "vue";
import { installController } from "../pwa/bootstrap.js";
import { httpsOrigin, installPresentation } from "../pwa/install.js";
import BearLogo from "../components/BearLogo.vue";

const state = shallowRef(installController.getState());
const unsubscribe = installController.subscribe((next) => { state.value = next; });
onBeforeUnmount(unsubscribe);
const presentation = computed(() => installPresentation(state.value));
const resourceLabel = computed(() => ({ idle: "尚未检查", checking: "检查中…", ready: "清单与图标可获取", error: "检查失败" })[state.value.resources]);
void installController.check();
function install() {
  // Only this explicit button gesture can consume the browser's one-shot event.
  void installController.promptInstall({ userGesture: true });
}

const httpsInput = ref("");
const destination = ref("");
const addressError = ref("");
function prepareAddress() {
  destination.value = "";
  addressError.value = "";
  try { destination.value = httpsOrigin(httpsInput.value); }
  catch (error) { addressError.value = error.message; }
}
function cancelAddress() {
  httpsInput.value = "";
  destination.value = "";
  addressError.value = "";
}
</script>

<template>
  <section class="h-full overflow-y-auto px-5 py-5 text-sm text-mactext">
    <div class="mx-auto max-w-2xl space-y-4">
      <header class="flex items-center gap-3">
        <span class="h-12 w-12 shrink-0"><BearLogo /></span>
        <div>
          <h1 class="text-base font-semibold">安装应用 (PWA)</h1>
          <p class="mt-0.5 text-xs text-macsub">把自己的 OpenBear 放到桌面或主屏幕</p>
        </div>
      </header>

      <div class="rounded-2xl border border-macborder bg-ob-surface/75 p-4">
        <dl class="space-y-3 text-xs">
          <div class="flex flex-wrap items-baseline gap-x-4 gap-y-1">
            <dt class="w-20 shrink-0 text-macsub">当前入口</dt>
            <dd class="min-w-0 break-all font-medium">{{ state.origin }}</dd>
          </div>
          <div class="flex flex-wrap items-baseline gap-x-4 gap-y-1">
            <dt class="w-20 shrink-0 text-macsub">安全上下文</dt>
            <dd>{{ state.secure ? '是（浏览器判定）' : '否' }}</dd>
          </div>
          <div class="flex flex-wrap items-baseline gap-x-4 gap-y-1">
            <dt class="w-20 shrink-0 text-macsub">安装资源</dt>
            <dd>{{ resourceLabel }}</dd>
          </div>
        </dl>
      </div>

      <div class="rounded-2xl border border-macborder bg-ob-surface/75 p-4">
        <div aria-live="polite" role="status">
          <h2 class="font-semibold">{{ presentation.title }}</h2>
          <p class="mt-2 text-xs leading-relaxed text-macsub">{{ presentation.detail }}</p>
        </div>
        <div class="mt-4 flex flex-wrap items-center gap-2">
          <el-button v-if="presentation.kind === 'available' || presentation.kind === 'prompting'" type="primary" round :loading="presentation.kind === 'prompting'" @click="install">安装 OpenBear</el-button>
          <el-button round :loading="state.resources === 'checking'" :disabled="presentation.kind === 'prompting'" @click="installController.check()">重新检查</el-button>
        </div>
      </div>

      <form v-if="state.http" class="rounded-2xl border border-macborder bg-ob-surface/75 p-4" @submit.prevent="prepareAddress">
        <h2 class="font-semibold">已有自己的 HTTPS 入口？</h2>
        <p id="pwa-https-help" class="mt-2 text-xs leading-relaxed text-macsub">仅填写你自己可信的 OpenBear 地址。这里只解析地址，不会在后台访问、验证或替你配置 HTTPS。跳转不复制当前会话或密钥，目标入口可能需要重新登录。</p>
        <template v-if="!destination">
          <label for="pwa-https-input" class="mt-3 block text-xs text-macsub">HTTPS 地址</label>
          <el-input id="pwa-https-input" v-model="httpsInput" class="mt-1" placeholder="https://bear.example.com" inputmode="url" autocomplete="off" autocapitalize="off" :spellcheck="false" aria-describedby="pwa-https-help pwa-https-error" />
          <p v-if="addressError" id="pwa-https-error" role="alert" class="mt-2 text-xs text-ob-danger">{{ addressError }}</p>
          <div class="mt-3 flex flex-wrap gap-2">
            <el-button native-type="submit" round>确认目标地址</el-button>
            <el-button native-type="button" text @click="cancelAddress">取消</el-button>
          </div>
        </template>
        <div v-else class="mt-3 space-y-3">
          <p class="text-xs leading-relaxed text-macsub">将只打开以下 origin，路径、查询参数与片段均已移除。请核对主机名，确认它属于你：</p>
          <p class="break-all font-medium">{{ destination }}</p>
          <div class="flex flex-wrap items-center gap-3">
            <a :href="destination" rel="noreferrer noopener" referrerpolicy="no-referrer" class="rounded-full bg-macblue px-4 py-2 text-xs font-medium text-ob-inverse focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2">前往此 HTTPS 入口</a>
            <el-button native-type="button" text @click="cancelAddress">取消</el-button>
          </div>
        </div>
      </form>

      <p class="px-1 text-xs leading-relaxed text-macsub">安装后仍连接当前同源服务，沿用现有登录认证。首期不提供离线使用、业务缓存或 Web Push。安装方式由浏览器决定；你始终可以继续使用网页版。</p>
    </div>
  </section>
</template>
