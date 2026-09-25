<script setup>
import { computed, onMounted, onBeforeUnmount, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Api, apiError } from "../api";
import { defineLazyView } from "../lazyView.js";

const TemplateView = defineLazyView(() => import("./TemplateView.vue"), "提示词模板");
const LogsView = defineLazyView(() => import("./LogsView.vue"), "系统日志");
const SettingsView = defineLazyView(() => import("./SettingsView.vue"), "系统设置");
const SessionsView = defineLazyView(() => import("./SessionsView.vue"), "登录设备");
const ChannelsView = defineLazyView(() => import("./ChannelsView.vue"), "渠道设置");
const RathAgentsView = defineLazyView(() => import("./RathAgentsView.vue"), "Agents");
const InstallAppView = defineLazyView(() => import("./InstallAppView.vue"), "安装应用 (PWA)");

const props = defineProps({ section: { type: String, default: "channels" } });
const emit = defineEmits(["section-changed", "mobile-header-ready"]);
// Keep the app navigation available while this lazy shell is loading or fails.
onMounted(() => emit("mobile-header-ready", true));
onBeforeUnmount(() => emit("mobile-header-ready", false));

const sections = [
  { key: "channels", label: "渠道设置", hint: "模型渠道、Key、协议和测试", icon: "Connection", component: ChannelsView },
  { key: "templates", label: "提示词模板", hint: "Prompt 模板与渲染素材", icon: "Document", component: TemplateView },
  { key: "agents", label: "Agents", hint: "子 Agent 池和预设能力", icon: "UserFilled", component: RathAgentsView },
  { key: "system-settings", label: "系统设置", hint: "运行配置、模型策略、记忆注入", icon: "Setting", component: SettingsView },
  { key: "sessions", label: "登录设备", hint: "查看并撤销浏览器登录", icon: "Key", component: SessionsView },
  { key: "logs", label: "系统日志", hint: "审计记录与操作追踪", icon: "List", component: LogsView },
  { key: "install-app", label: "安装应用 (PWA)", hint: "当前站点安装检查与指引", icon: "Download", component: InstallAppView },
];

function normalizeSection(value) {
  return sections.some((item) => item.key === value) ? value : "channels";
}

const activeSection = ref(normalizeSection(props.section));
const activeInfo = computed(() => sections.find((item) => item.key === activeSection.value) || sections[0]);
const activeComponent = computed(() => activeInfo.value.component);

const restarting = ref(false);

function runningSummary(r = {}) {
  return `OpenBear ${r.openbearRuns || 0} · Rath ${r.rathTasks || 0} · 子进程 ${r.childProcesses || 0} · 操作 ${r.operations || 0}`;
}

async function requestRestart(force = false) {
  restarting.value = true;
  try {
    const data = await Api.systemRestart({ confirm: true, force, reason: "web settings page" });
    if (data?.ok === false) throw { response: { status: 400, data } };
    ElMessage.success("已调度 OpenBear 重启");
  } catch (error) {
    const resp = error?.response;
    if (resp?.status === 409 && resp.data?.running) {
      const r = resp.data.running;
      try {
        await ElMessageBox.confirm(`当前仍有运行中任务：${runningSummary(r)}。确认强制重启？`, "系统繁忙", {
          type: "warning",
          confirmButtonText: "强制重启",
          cancelButtonText: "取消"
        });
      } catch { return; }
      return requestRestart(true);
    }
    ElMessage.error(apiError(error));
  } finally {
    restarting.value = false;
  }
}

async function confirmRestart() {
  try {
    await ElMessageBox.confirm("确认重启 OpenBear？当前 Web 会短暂断开，重启完成后会通过 Telegram 发通知。", "重启确认", {
      type: "warning",
      confirmButtonText: "重启",
      cancelButtonText: "取消"
    });
  } catch { return; }
  await requestRestart(false);
}

function selectSection(key) {
  const next = normalizeSection(key);
  if (activeSection.value === next) return;
  activeSection.value = next;
}

watch(() => props.section, (next) => {
  const normalized = normalizeSection(next);
  if (activeSection.value !== normalized) activeSection.value = normalized;
});
watch(activeSection, (next) => emit("section-changed", next));
</script>

<template>
  <section class="settings-hub h-full min-h-0 flex flex-col bg-macbg">
    <header class="settings-header shrink-0 border-b border-macborder bg-ob-surface/75 px-5 py-3 backdrop-blur">
      <div class="settings-header-row flex min-w-0 items-center gap-4">
        <div class="settings-mobile-navigation"><slot name="mobile-navigation" /></div>
        <div class="settings-heading shrink-0">
          <div class="text-base font-semibold leading-tight text-mactext">设置</div>
          <div class="settings-subtitle mt-0.5 text-[11px] leading-tight text-macsub">系统功能入口</div>
        </div>
        <div class="settings-section-control">
        <select
          class="settings-section-select text-sm font-medium text-mactext"
          aria-label="切换设置分区"
          :value="activeSection"
          @change="selectSection($event.target.value)"
        >
          <option v-for="item in sections" :key="item.key" :value="item.key">{{ item.label }}</option>
        </select>
        <svg class="settings-section-chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="m4.5 6 3.5 3.5L11.5 6" /></svg>
        </div>
        <nav class="settings-desktop-tabs min-w-0 flex-1 overflow-x-auto">
          <div class="flex w-max items-center gap-1 rounded-2xl bg-ob-soft/80 p-1 ring-1 ring-inset ring-ob-border">
            <button
              v-for="item in sections"
              :key="item.key"
              type="button"
              class="flex items-center gap-1.5 rounded-xl px-3 py-2 text-sm font-medium transition whitespace-nowrap"
              :class="activeSection === item.key ? 'bg-ob-surface text-ob-strong shadow-sm ring-1 ring-ob-border' : 'text-ob-subtle hover:bg-ob-surface/70 hover:text-ob-strong'"
              :title="item.hint"
              @click="selectSection(item.key)"
            >
              <el-icon :size="15"><component :is="item.icon" /></el-icon>
              <span>{{ item.label }}</span>
            </button>
          </div>
        </nav>
        <el-button class="settings-restart" type="danger" plain round :loading="restarting" :aria-busy="restarting" aria-label="重启 OpenBear" title="重启 OpenBear" @click="confirmRestart"><span>重启<span class="settings-restart-product"> OpenBear</span></span></el-button>
      </div>
    </header>
    <div class="settings-content min-h-0 flex-1">
      <component :is="activeComponent" />
    </div>
  </section>
</template>

<style scoped>
.settings-section-select, .settings-section-control, .settings-mobile-navigation { display: none; }
@media (max-width: 760px) {
  .settings-hub { min-width: 0; }
  .settings-header { padding: calc(4px + env(safe-area-inset-top, 0px)) max(12px, env(safe-area-inset-right, 0px)) 4px max(8px, env(safe-area-inset-left, 0px)); background: var(--el-bg-color-overlay); }
  .settings-header-row { gap: 8px; }
  .settings-mobile-navigation { display: flex; flex: 0 0 auto; }
  .settings-section-control { display: block; position: relative; flex: 1 1 0%; min-width: 0; }
  .settings-section-chevron { position: absolute; right: 14px; top: calc(50% - 8px); width: 16px; height: 16px; fill: none; stroke: currentColor; stroke-width: 1.5; stroke-linecap: round; stroke-linejoin: round; pointer-events: none; color: var(--el-text-color-secondary); }
  .settings-heading,
  .settings-subtitle,
  .settings-desktop-tabs,
  .settings-restart-product { display: none; }
  .settings-section-select {
    display: block;
    width: 100%;
    appearance: none;
    -webkit-appearance: none;
    background-image: none;
    min-width: 0;
    min-height: 44px;
    border: 1px solid var(--el-border-color-lighter);
    border-radius: 10px;
    background-color: var(--el-fill-color-light);
    padding: 0 40px 0 12px;
    cursor: pointer;
  }
  .settings-header .settings-restart {
    flex: 0 0 auto;
    min-width: 44px;
    min-height: 44px;
    margin: 0;
    padding: 0 8px;
    border: 0;
    border-radius: 9px;
    background: transparent;
    color: var(--el-text-color-secondary);
    box-shadow: none;
  }
  .settings-header .settings-restart:hover:not(:disabled) { color: var(--el-color-danger); }
  .settings-section-select:focus-visible,
  .settings-header .settings-restart:focus-visible { outline: 2px solid var(--el-color-primary); outline-offset: -2px; }
  .settings-content { min-width: 0; }
}
</style>
