// One submission owns its polling chain. Disposed/older requests cannot change the UI.
export function createLoginFlow({ state, api, success, error, authenticated,
  schedule = (fn, ms) => window.setTimeout(fn, ms),
  unschedule = (id) => window.clearTimeout(id), now = () => Date.now(),
}) {
  let timer = null;
  let generation = 0;
  let disposed = false;
  let deadline = 0;
  const current = (id) => !disposed && id === generation;
  const clearPoll = () => {
    if (timer !== null) unschedule(timer);
    timer = null;
  };
  const finish = (status, message) => {
    clearPoll();
    state.loading = false;
    state.requestUuid = "";
    state.status = status;
    if (message) error(message);
  };
  const failureMessage = (err) => {
    const code = err?.response?.data?.error;
    if (code === "https_required") return "当前入口无法安全保存登录 Cookie，请使用配置的 HTTPS 地址重新打开登录页。";
    return code || err?.response?.data?.message || err?.message || String(err);
  };

  async function checkSession(id, consumed = false) {
    try {
      await api.authSession();
      if (current(id)) authenticated();
    } catch {
      if (current(id)) finish("missing", consumed
        ? "本次请求已处理，但当前浏览器没有有效登录会话，请重新登录。"
        : "浏览器未保存有效登录 Cookie，请检查访问地址与 Cookie 设置后重新登录。");
    }
  }

  async function poll(id, uuid) {
    if (!current(id)) return;
    if (now() >= deadline) {
      finish("expired", "登录请求已过期，请重新输入 Secret Key");
      return;
    }
    try {
      const result = await api.loginStatus(uuid);
      if (!current(id)) return;
      state.status = result.status;
      switch (result.status) {
        case "approved":
          await api.consumeLogin(uuid);
          if (current(id)) await checkSession(id);
          return;
        case "consumed":
          await checkSession(id, true);
          return;
        case "rejected":
        case "denied":
          finish(result.status, "Telegram 已拒绝本次登录");
          return;
        case "expired":
          finish("expired", "登录请求已过期，请重新输入 Secret Key");
          return;
        case "missing":
          finish("missing", "登录请求已失效，请重新登录");
          return;
        case "pending":
          timer = schedule(() => poll(id, uuid), 1800);
          return;
        default:
          finish("invalid", "无法识别登录请求状态，请重新登录");
      }
    } catch (err) {
      if (current(id)) finish("error", failureMessage(err));
    }
  }

  return {
    async submit() {
      if (disposed || state.loading || !state.secret) return;
      const id = ++generation;
      clearPoll();
      state.retryAfter = 0;
      state.requestUuid = "";
      state.status = "starting";
      state.loading = true;
      try {
        const result = await api.loginStart(state.secret);
        if (!current(id)) return;
        if (!result.requestUuid) throw new Error("登录服务未返回有效请求，请重试");
        state.requestUuid = result.requestUuid;
        state.status = "pending";
        deadline = now() + Math.min(3600, Math.max(60, Number(result.expiresIn) || 300)) * 1000;
        success("Secret Key 已通过，请在 Telegram 中确认登录");
        timer = schedule(() => poll(id, result.requestUuid), 800);
      } catch (err) {
        if (!current(id)) return;
        state.retryAfter = Number(err?.response?.data?.retryAfter || 0);
        finish("error", failureMessage(err));
      }
    },
    dispose() {
      disposed = true;
      generation += 1;
      clearPoll();
    },
  };
}
