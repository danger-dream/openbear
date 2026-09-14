import { createLoginLifecycle } from "./loginLifecycle.js";

const POLL_MS = 1800;
const MAX_FAILURES = 6;
const SESSION_RECOVERY_MS = 60_000;
const transient = (err) => {
  const status = err?.response?.status;
  if (status) return [408, 500, 502, 503, 504].includes(status);
  return !err?.response && (Boolean(err?.request) || err?.isAxiosError === true
    || ["ERR_NETWORK", "ECONNABORTED", "ETIMEDOUT"].includes(err?.code));
};
const rateLimited = (err) => err?.response?.status === 429 || err?.response?.data?.error === "rate_limited";

// One in-memory submission owns one start, at most one consume, and one polling chain.
// Status is NOT nonce-bound on the server: do not restore a locator across reloads.
export function createLoginFlow({ state, api, success, error, authenticated,
  schedule = (fn, ms) => window.setTimeout(fn, ms),
  unschedule = (id) => window.clearTimeout(id), now = () => Date.now(),
  lifecycle = createLoginLifecycle(),
}) {
  let timer = null;
  let generation = 0;
  let disposed = false;
  let inFlight = false;
  let deadline = 0;
  let cooldownUntil = 0;
  let retryAt = 0;
  let lastAttemptAt = -Infinity;
  let failures = 0;
  let phase = "idle";
  let sessionReason = "cookie";
  const current = (id) => !disposed && id === generation;
  const available = () => lifecycle.isVisible() && lifecycle.isOnline();
  let wasAvailable = available();
  const clearPoll = () => {
    if (timer !== null) unschedule(timer);
    timer = null;
  };
  const finish = (status, message) => {
    clearPoll();
    phase = "idle";
    state.loading = false;
    state.requestUuid = "";
    state.status = status;
    state.notice = "";
    if (message) error(message);
  };
  const failureMessage = (err) => {
    const code = err?.response?.data?.error;
    if (code === "https_required") return "当前入口无法安全保存登录 Cookie，请使用配置的 HTTPS 地址重新打开登录页。";
    if (code === "bad_secret") return "Secret Key 不正确，请检查后重试。";
    // Never echo an Axios error/config, server body or URL that might contain credentials.
    return "登录请求失败，请检查网络与访问地址后重新登录。";
  };
  const missingSessionMessage = () => sessionReason === "uncertain"
    ? "未能确认本次登录已完成，当前浏览器没有有效会话；为避免重复消费，请重新登录。"
    : sessionReason === "consumed"
      ? "本次请求已处理，但当前浏览器没有有效登录会话，请重新登录。"
      : "浏览器未保存有效登录 Cookie，请检查访问地址与 Cookie 设置后重新登录。";
  const expire = () => finish("expired", phase === "session"
    ? "登录会话确认已超时，未重复消费登录请求，请重新登录。"
    : "登录请求已过期，请重新输入 Secret Key");
  const pauseNotice = () => {
    state.notice = !lifecycle.isOnline()
      ? "网络已断开，连接恢复后将继续确认；不会重复提交登录。"
      : "已暂停后台查询，返回页面后会继续确认。";
  };
  const refreshCooldown = () => {
    state.retryAfter = Math.max(0, Math.ceil((cooldownUntil - now()) / 1000));
  };
  const coolDown = (err) => {
    const headers = err?.response?.headers;
    const raw = err?.response?.data?.retryAfter || headers?.["retry-after"] || headers?.get?.("Retry-After");
    const numeric = Number(raw);
    const seconds = Number.isFinite(numeric) ? numeric : (Date.parse(raw) - now()) / 1000;
    cooldownUntil = now() + (Number.isFinite(seconds) && seconds > 0 ? Math.ceil(seconds) : 60) * 1000;
    refreshCooldown();
    finish("error", "登录请求过于频繁，请等待冷却后再试。");
  };

  function queue(id, delay) {
    clearPoll();
    if (!current(id) || !state.loading || phase === "idle") return;
    if (!available()) { pauseNotice(); return; }
    const scheduled = schedule(() => {
      if (timer !== scheduled) return; // A cleared callback must not steal a newer timer.
      timer = null;
      return poll(id);
    }, Math.max(0, Math.min(delay, deadline - now())));
    timer = scheduled;
  }

  function beginSession(reason) {
    phase = "session"; // Set BEFORE consume: an uncertain response must never replay that POST.
    sessionReason = reason;
    deadline = now() + SESSION_RECOVERY_MS;
    failures = 0;
    retryAt = 0;
    state.status = "verifying";
    state.notice = "正在确认浏览器登录会话…";
  }

  async function checkSession(id) {
    if (!available()) { pauseNotice(); return; }
    const result = await api.authSession();
    if (!current(id)) return;
    if (result?.ok !== true) {
      finish("missing", missingSessionMessage());
      return;
    }
    finish("authenticated");
    authenticated();
  }

  async function readStatus(id) {
    const result = await api.loginStatus(state.requestUuid);
    if (!current(id)) return;
    if (now() >= deadline) { expire(); return; }
    failures = 0;
    retryAt = 0;
    state.status = result.status;
    state.notice = "";
    switch (result.status) {
      case "approved":
        // A pending status read may settle while the user is in Telegram.
        if (!available()) return;
        beginSession("cookie");
        try {
          await api.consumeLogin(state.requestUuid);
        } catch (err) {
          if (!current(id)) return;
          if (err?.response && !transient(err)) throw err;
          sessionReason = "uncertain";
          state.notice = "登录响应暂未确认，正在检查现有会话；不会重复提交批准请求。";
        }
        if (current(id)) await checkSession(id);
        return;
      case "consumed":
        beginSession("consumed");
        await checkSession(id);
        return;
      case "rejected":
      case "denied":
        finish(result.status, "Telegram 已拒绝本次登录");
        return;
      case "expired":
        expire();
        return;
      case "missing":
        finish("missing", "登录请求已失效，请重新登录");
        return;
      case "pending":
        return;
      default:
        finish("invalid", "无法识别登录请求状态，请重新登录");
    }
  }

  async function poll(id) {
    if (!current(id) || !state.loading || inFlight || phase === "idle") return;
    clearPoll();
    if (!available()) { pauseNotice(); return; }
    if (now() >= deadline) { expire(); return; }
    // Lifecycle event bursts cannot bypass the normal read cadence or retry backoff.
    const wait = Math.max(retryAt, lastAttemptAt + POLL_MS) - now();
    if (wait > 0) { queue(id, wait); return; }
    inFlight = true;
    lastAttemptAt = now();
    try {
      if (phase === "session") await checkSession(id);
      else await readStatus(id);
    } catch (err) {
      if (!current(id)) return;
      if (rateLimited(err)) coolDown(err);
      else if (transient(err)) {
        failures += 1;
        if (now() >= deadline) expire();
        else if (failures >= MAX_FAILURES) finish("error", "网络或登录服务仍不可用，本次自动恢复已停止，请稍后重新登录。");
        else {
          retryAt = now() + Math.min(15_000, POLL_MS * 2 ** (failures - 1));
          state.notice = phase === "session"
            ? "暂时无法确认登录会话，稍后自动检查；不会重复消费登录请求。"
            : "网络或登录服务暂不可用，稍后自动继续等待 Telegram 确认。";
        }
      } else if (phase === "session" && err?.response?.data?.error !== "https_required") {
        finish("missing", missingSessionMessage());
      } else finish("error", failureMessage(err));
    } finally {
      inFlight = false;
      if (current(id) && state.loading) queue(id, Math.max(POLL_MS, retryAt - now()));
    }
  }

  const unsubscribe = lifecycle.subscribe(() => {
    if (disposed) return;
    const ready = available();
    const resumed = ready && !wasAvailable;
    wasAvailable = ready;
    refreshCooldown();
    if (!state.loading || phase === "idle") return;
    if (!ready) { clearPoll(); pauseNotice(); }
    else if (resumed && !inFlight) {
      state.notice = phase === "session" ? "正在恢复登录会话确认…" : "已返回页面，正在确认 Telegram 登录状态…";
      // poll handles expiry/backoff and starts at most one read; no secret/consume replay.
      void poll(generation);
    }
  });

  return {
    async submit() {
      if (disposed || state.loading || inFlight || !state.secret) return;
      refreshCooldown();
      if (state.retryAfter) return;
      const id = ++generation;
      clearPoll();
      state.requestUuid = "";
      state.status = "starting";
      state.notice = "";
      state.loading = true;
      phase = "idle";
      failures = 0;
      retryAt = 0;
      lastAttemptAt = -Infinity;
      inFlight = true;
      const startedAt = now();
      try {
        // Only explicit submit calls this POST. A lost start response cannot be recovered safely.
        const result = await api.loginStart(state.secret);
        if (!current(id)) return;
        if (!result.requestUuid) { finish("invalid", "登录服务未返回有效请求，请重试"); return; }
        state.requestUuid = result.requestUuid;
        state.status = "pending";
        phase = "status";
        const ttl = Number(result.expiresIn);
        deadline = startedAt + (Number.isFinite(ttl) && ttl > 0 ? Math.min(3600, ttl) : 300) * 1000;
        if (now() >= deadline) { expire(); return; }
        success("Secret Key 已通过，请在 Telegram 中确认登录");
      } catch (err) {
        if (!current(id)) return;
        if (rateLimited(err)) coolDown(err);
        else finish("error", transient(err)
          ? "无法确认登录请求是否已送达，未自动重试。请网络恢复后手动重新登录。"
          : failureMessage(err));
      } finally {
        inFlight = false;
        if (current(id) && state.loading) queue(id, 800);
      }
    },
    dispose() {
      disposed = true;
      generation += 1;
      clearPoll();
      unsubscribe();
    },
  };
}
