<script setup>
import { computed, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Api, apiError } from "../api";
import TemplateView from "./TemplateView.vue";
import LogsView from "./LogsView.vue";
import SettingsView from "./SettingsView.vue";
import ChannelsView from "./ChannelsView.vue";
import RathAgentsView from "./RathAgentsView.vue";

const props = defineProps({ section: { type: String, default: "channels" } });
const emit = defineEmits(["section-changed"]);

const sections = [
  { key: "channels", label: "渠道设置", hint: "模型渠道、Key、协议和测试", icon: "Connection", component: ChannelsView },
  { key: "templates", label: "提示词模板", hint: "Prompt 模板与渲染素材", icon: "Document", component: TemplateView },
  { key: "agents", label: "Agents", hint: "子 Agent 池和预设能力", icon: "UserFilled", component: RathAgentsView },
  { key: "system-settings", label: "系统设置", hint: "运行配置、模型策略、记忆注入", icon: "Setting", component: SettingsView },
  { key: "logs", label: "系统日志", hint: "审计记录与操作追踪", icon: "List", component: LogsView },
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
  <section class="h-full min-h-0 flex flex-col bg-macbg">
    <header class="shrink-0 border-b border-macborder bg-white/75 px-5 py-3 backdrop-blur">
      <div class="flex min-w-0 items-center gap-4">
        <div class="shrink-0">
          <div class="text-base font-semibold leading-tight text-mactext">设置</div>
          <div class="mt-0.5 text-[11px] leading-tight text-macsub">系统功能入口</div>
        </div>
        <nav class="min-w-0 flex-1 overflow-x-auto">
          <div class="flex w-max items-center gap-1 rounded-2xl bg-zinc-100/80 p-1 ring-1 ring-inset ring-zinc-200/70">
            <button
              v-for="item in sections"
              :key="item.key"
              type="button"
              class="flex items-center gap-1.5 rounded-xl px-3 py-2 text-sm font-medium transition whitespace-nowrap"
              :class="activeSection === item.key ? 'bg-white text-zinc-950 shadow-sm ring-1 ring-zinc-200' : 'text-zinc-600 hover:bg-white/70 hover:text-zinc-950'"
              :title="item.hint"
              @click="selectSection(item.key)"
            >
              <el-icon :size="15"><component :is="item.icon" /></el-icon>
              <span>{{ item.label }}</span>
            </button>
          </div>
        </nav>
        <el-button type="danger" plain round :loading="restarting" @click="confirmRestart">重启 OpenBear</el-button>
      </div>
    </header>
    <div class="min-h-0 flex-1">
      <component :is="activeComponent" />
    </div>
  </section>
</template>
