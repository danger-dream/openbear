<script setup>
import { computed, onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { Api, apiError } from "../api";
import {
  describeSessionUserAgent,
  formatSessionTime,
  relativeSessionTime,
} from "../sessionPresentation.js";

const loading = ref(false);
const revokingId = ref(0);
const sessions = ref([]);

const activeCount = computed(() => sessions.value.length);
const otherCount = computed(() => sessions.value.filter((item) => !item.current).length);

function okOrThrow(data) {
  if (data?.ok === false) throw new Error(data.error || "操作失败");
  return data;
}

function sessionDevice(item) {
  return describeSessionUserAgent(item?.userAgent || "");
}

async function loadSessions() {
  if (loading.value) return;
  loading.value = true;
  try {
    const data = okOrThrow(await Api.authSessions());
    sessions.value = Array.isArray(data.items) ? data.items : [];
  } catch (error) {
    ElMessage.error(apiError(error));
  } finally {
    loading.value = false;
  }
}

async function revokeSession(item) {
  if (!item || revokingId.value) return;
  const device = sessionDevice(item);
  const title = item.current ? "退出当前设备？" : "撤销这台设备的登录？";
  const message = item.current
    ? "撤销后当前页面会立即回到登录页，其他设备不受影响。"
    : `${device.label} 将立即失去访问权限，再次使用时需要重新登录。`;
  try {
    await ElMessageBox.confirm(message, title, {
      type: "warning",
      confirmButtonText: item.current ? "退出当前设备" : "撤销登录",
      cancelButtonText: "取消",
      distinguishCancelAndClose: true,
    });
  } catch {
    return;
  }

  revokingId.value = Number(item.id);
  try {
    const data = okOrThrow(await Api.revokeAuthSession(item.id));
    if (data.current) {
      window.location.replace("/login");
      return;
    }
    sessions.value = sessions.value.filter((entry) => Number(entry.id) !== Number(item.id));
    ElMessage.success("该设备的登录已撤销");
  } catch (error) {
    if (error?.response?.data?.error === "session_not_found") {
      sessions.value = sessions.value.filter((entry) => Number(entry.id) !== Number(item.id));
      ElMessage.info("该登录已经失效或被撤销");
    } else {
      ElMessage.error(apiError(error));
    }
  } finally {
    revokingId.value = 0;
  }
}

onMounted(loadSessions);
</script>

<template>
  <section class="sessions-view h-full min-h-0 flex flex-col bg-macbg" v-loading="loading">
    <header class="sessions-header h-14 shrink-0 flex items-center justify-between gap-4 px-3 sm:px-6 border-b border-macborder bg-ob-surface/75 backdrop-blur">
      <div class="min-w-0">
        <div class="flex min-w-0 items-center gap-2">
          <h1 class="text-base font-semibold whitespace-nowrap">登录设备</h1>
          <span class="hidden md:inline text-xs text-macsub truncate">查看并撤销仍可访问 OpenBear 的浏览器登录</span>
        </div>
      </div>
      <button class="sessions-toolbar-button" type="button" :disabled="loading" title="刷新登录设备" @click="loadSessions">
        <el-icon :class="{ 'is-spinning': loading }"><RefreshRight /></el-icon>
        <span>刷新</span>
      </button>
    </header>

    <main class="sessions-content flex-1 min-h-0 overflow-y-auto p-3 sm:p-4 lg:p-6">
      <div class="sessions-workspace">
        <section class="sessions-overview" aria-label="登录设备概况">
          <div>
            <span>有效会话</span>
            <strong>{{ activeCount }}</strong>
          </div>
          <div>
            <span>其他设备</span>
            <strong>{{ otherCount }}</strong>
          </div>
          <p><el-icon><InfoFilled /></el-icon>撤销只影响目标浏览器，不会删除聊天内容或其他设备上的登录。</p>
        </section>

        <section class="sessions-panel">
          <header class="sessions-panel__header">
            <div>
              <h2>有效会话</h2>
              <p>当前设备排在第一位；最近活动时间最多每 5 分钟更新一次。</p>
            </div>
            <span>{{ activeCount }} 个</span>
          </header>

          <div v-if="sessions.length" class="sessions-list">
            <article
              v-for="item in sessions"
              :key="item.id"
              class="session-device"
              :class="{ 'is-current': item.current }"
            >
              <div class="session-device__symbol" aria-hidden="true">
                <el-icon :size="20">
                  <Iphone v-if="sessionDevice(item).deviceKind === 'mobile'" />
                  <Monitor v-else />
                </el-icon>
              </div>

              <div class="session-device__content">
                <div class="session-device__title">
                  <h3>{{ sessionDevice(item).label }}</h3>
                  <span v-if="item.current" class="session-current-badge"><i></i>当前设备</span>
                </div>

                <div class="session-device__identity">
                  <span><el-icon><Location /></el-icon>{{ item.ip || 'IP 未记录' }}</span>
                  <span :title="item.userAgent || 'User-Agent 未记录'"><el-icon><Compass /></el-icon>{{ item.userAgent ? '浏览器信息已记录' : '浏览器信息未记录' }}</span>
                </div>

                <dl class="session-device__times">
                  <div>
                    <dt>最近活动</dt>
                    <dd :title="formatSessionTime(item.lastSeenAt)">{{ relativeSessionTime(item.lastSeenAt) }}</dd>
                  </div>
                  <div>
                    <dt>登录时间</dt>
                    <dd>{{ formatSessionTime(item.createdAt) }}</dd>
                  </div>
                  <div>
                    <dt>有效期至</dt>
                    <dd>{{ formatSessionTime(item.expiresAt) }}</dd>
                  </div>
                </dl>
              </div>

              <button
                class="session-revoke"
                :class="{ 'is-current': item.current }"
                type="button"
                :disabled="Boolean(revokingId)"
                @click="revokeSession(item)"
              >
                <el-icon v-if="revokingId === Number(item.id)" class="is-spinning"><Loading /></el-icon>
                <el-icon v-else><SwitchButton /></el-icon>
                <span>{{ revokingId === Number(item.id) ? '处理中…' : (item.current ? '退出本机' : '撤销登录') }}</span>
              </button>
            </article>
          </div>

          <div v-else-if="!loading" class="sessions-empty">
            <el-icon :size="26"><CircleCheck /></el-icon>
            <strong>没有有效会话</strong>
            <p>刷新后仍为空时，请重新登录。</p>
          </div>
        </section>
      </div>
    </main>
  </section>
</template>

<style scoped>
.sessions-view {
  --session-ink: var(--ob-text-strong);
  --session-muted: var(--ob-text-subtle);
  --session-faint: var(--ob-text-muted);
  --session-line: var(--ob-border);
  --session-surface: var(--ob-surface);
  --session-inset: var(--ob-surface-soft);
  --session-accent: var(--ob-blue);
  --session-accent-wash: var(--ob-blue-soft);
  color: var(--session-ink);
}
.sessions-header { color: var(--session-ink); }
.sessions-toolbar-button,
.session-revoke {
  display: inline-flex;
  min-height: 32px;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 0 12px;
  border: 1px solid var(--ob-border);
  border-radius: 999px;
  background: linear-gradient(180deg, rgb(var(--ob-surface-rgb) / .94), rgb(var(--ob-surface-rgb) / .88));
  color: var(--ob-text);
  font-size: 12px;
  font-weight: 500;
  box-shadow: inset 0 1px 0 var(--ob-border), 0 1px 2px rgb(var(--ob-shadow-rgb) / .04);
  transition: transform .14s ease, box-shadow .14s ease, border-color .14s ease, opacity .14s ease;
}
.sessions-toolbar-button:hover:not(:disabled),
.session-revoke:hover:not(:disabled) {
  transform: translateY(-1px);
  box-shadow: inset 0 1px 0 var(--ob-border), 0 6px 16px rgb(var(--ob-shadow-rgb) / .08);
}
.sessions-toolbar-button:disabled,
.session-revoke:disabled { cursor: not-allowed; opacity: .48; }
.sessions-workspace { width: min(1040px, 100%); margin: 0 auto; }
.sessions-overview {
  display: flex;
  align-items: center;
  gap: 0;
  overflow: hidden;
  margin-bottom: 14px;
  border: 1px solid var(--session-line);
  border-radius: 14px;
  background: var(--session-surface);
  box-shadow: 0 2px 8px rgb(var(--ob-shadow-rgb) / .025), inset 0 1px 0 var(--ob-border);
}
.sessions-overview > div {
  display: flex;
  min-width: 140px;
  align-items: baseline;
  gap: 9px;
  padding: 11px 16px;
  border-right: 1px solid var(--session-line);
}
.sessions-overview span { color: var(--session-muted); font-size: 11px; }
.sessions-overview strong { color: var(--ob-text); font-size: 15px; font-weight: 650; }
.sessions-overview p {
  display: flex;
  min-width: 0;
  flex: 1;
  align-items: center;
  gap: 7px;
  padding: 0 16px;
  color: var(--session-muted);
  font-size: 11px;
  line-height: 1.5;
}
.sessions-overview p .el-icon { flex: 0 0 auto; color: var(--session-faint); }
.sessions-panel {
  overflow: hidden;
  border: 1px solid var(--session-line);
  border-radius: 18px;
  background: var(--session-surface);
  box-shadow: 0 6px 18px rgb(var(--ob-shadow-rgb) / .04), inset 0 1px 0 var(--ob-border);
}
.sessions-panel__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 13px 17px 11px;
  border-bottom: 1px solid var(--ob-border);
  background: linear-gradient(180deg, rgb(var(--ob-surface-rgb) / .88), rgb(var(--ob-surface-rgb) / .58));
}
.sessions-panel__header h2 { font-size: 14px; font-weight: 700; letter-spacing: -.01em; }
.sessions-panel__header p { margin-top: 2px; color: var(--session-faint); font-size: 11px; }
.sessions-panel__header > span {
  padding: 3px 8px;
  border: 1px solid var(--ob-border);
  border-radius: 999px;
  background: rgb(var(--ob-surface-rgb) / .8);
  color: var(--session-muted);
  font-size: 11px;
}
.session-device {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 14px;
  padding: 16px 17px;
  border-bottom: 1px solid var(--ob-border);
  transition: background-color .12s ease;
}
.session-device:last-child { border-bottom: 0; }
.session-device:hover { background: rgb(var(--ob-surface-rgb) / .76); }
.session-device.is-current {
  background: linear-gradient(90deg, var(--session-accent-wash), transparent 72%);
  box-shadow: inset 3px 0 0 rgb(var(--ob-blue-rgb) / .72);
}
.session-device__symbol {
  display: grid;
  width: 40px;
  height: 40px;
  place-items: center;
  border: 1px solid var(--ob-border);
  border-radius: 13px;
  background: linear-gradient(180deg, rgb(var(--ob-surface-rgb) / .96), rgb(var(--ob-surface-rgb) / .9));
  color: var(--ob-text-subtle);
  box-shadow: inset 0 1px 0 var(--ob-border), 0 1px 3px rgb(var(--ob-shadow-rgb) / .06);
}
.session-device.is-current .session-device__symbol {
  border-color: rgb(var(--ob-blue-rgb) / .2);
  background: rgb(var(--ob-blue-rgb) / .07);
  color: var(--session-accent);
}
.session-device__content { min-width: 0; }
.session-device__title { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.session-device__title h3 { color: var(--ob-text); font-size: 14px; font-weight: 650; }
.session-current-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 7px;
  border-radius: 999px;
  background: rgb(var(--ob-blue-rgb) / .08);
  color: var(--ob-blue);
  font-size: 10px;
  font-weight: 700;
}
.session-current-badge i {
  width: 6px;
  height: 6px;
  border-radius: 99px;
  background: var(--ob-success);
  box-shadow: 0 0 0 3px rgb(var(--ob-success-rgb) / .11);
}
.session-device__identity {
  display: flex;
  min-width: 0;
  flex-wrap: wrap;
  gap: 6px 14px;
  margin-top: 5px;
  color: var(--session-muted);
  font-size: 11px;
}
.session-device__identity span { display: inline-flex; min-width: 0; align-items: center; gap: 4px; }
.session-device__identity .el-icon { color: var(--session-faint); }
.session-device__times { display: flex; flex-wrap: wrap; gap: 8px 22px; margin-top: 10px; }
.session-device__times div { display: grid; gap: 2px; }
.session-device__times dt { color: var(--session-faint); font-size: 9px; letter-spacing: .04em; }
.session-device__times dd { color: var(--ob-text); font: 11px/1.35 ui-monospace, SFMono-Regular, Menlo, monospace; }
.session-revoke { min-width: 104px; color: var(--ob-danger); border-color: rgb(var(--ob-danger-rgb) / .22); }
.session-revoke.is-current { color: var(--ob-text); border-color: var(--ob-border); }
.sessions-empty { display: grid; min-height: 220px; place-items: center; align-content: center; gap: 7px; color: var(--session-faint); }
.sessions-empty strong { color: var(--ob-text); font-size: 12px; }
.sessions-empty p { color: var(--session-faint); font-size: 10px; }
.is-spinning { animation: session-spin .8s linear infinite; }
@keyframes session-spin { to { transform: rotate(360deg); } }

@media (max-width: 760px) {
  .sessions-header { min-height: 56px; }
  .sessions-header .text-macsub { display: none; }
  .sessions-toolbar-button { min-width: 44px; min-height: 44px; padding: 0 10px; border: 0; border-radius: 9px; background: transparent; box-shadow: none; color: var(--session-muted); }
  .sessions-content { padding: 10px; }
  .sessions-overview { display: grid; grid-template-columns: 1fr 1fr; border-radius: 14px; }
  .sessions-overview > div { min-width: 0; justify-content: space-between; padding: 10px 12px; }
  .sessions-overview > div:nth-child(2) { border-right: 0; }
  .sessions-overview p { grid-column: 1 / -1; padding: 9px 12px; border-top: 1px solid var(--session-line); }
  .sessions-panel { border-radius: 16px; }
  .sessions-panel__header { align-items: flex-start; padding: 12px 13px; }
  .sessions-panel__header p { max-width: 240px; line-height: 1.45; }
  .session-device { grid-template-columns: auto minmax(0, 1fr); gap: 11px; padding: 14px 13px; }
  .session-device__symbol { width: 38px; height: 38px; border-radius: 12px; }
  .session-device__times { gap: 10px 18px; }
  .session-revoke { grid-column: 1 / -1; width: 100%; min-height: 44px; }
}

:global(html.dark) .sessions-view {
  --session-ink: var(--ob-text-strong);
  --session-muted: var(--ob-text-subtle);
  --session-faint: var(--ob-text-muted);
  --session-line: var(--ob-border);
  --session-surface: var(--ob-surface);
  --session-inset: var(--ob-surface-soft);
  --session-accent: var(--ob-blue);
  --session-accent-wash: var(--ob-blue-soft);
}
:global(html.dark) .sessions-header { border-bottom-color: var(--ob-border); background: rgb(var(--ob-surface-soft-rgb) / .82); }
:global(html.dark) .sessions-toolbar-button,
:global(html.dark) .session-revoke { border-color: var(--ob-border); background: linear-gradient(180deg, rgb(var(--ob-surface-soft-rgb) / .94), rgb(var(--ob-surface-soft-rgb) / .88)); color: var(--ob-text); box-shadow: inset 0 1px 0 rgb(var(--ob-border-rgb) / .11), 0 1px 2px rgb(var(--ob-shadow-rgb) / .16); }
:global(html.dark) .session-revoke:not(.is-current) { color: var(--ob-danger); border-color: rgb(var(--ob-danger-rgb) / .22); }
:global(html.dark) .sessions-overview,
:global(html.dark) .sessions-panel { box-shadow: 0 6px 18px rgb(var(--ob-shadow-rgb) / .16), inset 0 1px 0 rgb(var(--ob-border-rgb) / .05); }
:global(html.dark) .sessions-overview strong,
:global(html.dark) .session-device__title h3 { color: var(--ob-text-strong); }
:global(html.dark) .sessions-panel__header { border-bottom-color: var(--ob-border); background: linear-gradient(180deg, rgb(var(--ob-surface-soft-rgb) / .88), rgb(var(--ob-surface-soft-rgb) / .58)); }
:global(html.dark) .sessions-panel__header > span { border-color: var(--ob-border); background: rgb(var(--ob-surface-soft-rgb) / .8); color: var(--ob-text); }
:global(html.dark) .session-device { border-bottom-color: var(--ob-border); }
:global(html.dark) .session-device:hover { background: rgb(var(--ob-surface-soft-rgb) / .76); }
:global(html.dark) .session-device__symbol { border-color: var(--ob-border); background: linear-gradient(180deg, rgb(var(--ob-surface-soft-rgb) / .94), rgb(var(--ob-surface-soft-rgb) / .88)); color: var(--ob-text); box-shadow: inset 0 1px 0 rgb(var(--ob-border-rgb) / .08), 0 1px 3px rgb(var(--ob-shadow-rgb) / .16); }
:global(html.dark) .session-device__times dd { color: var(--ob-text); }
</style>
