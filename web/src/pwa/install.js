// Installation capability only: no service worker, storage, API or offline cache.
export const MANIFEST_PATH = "/manifest.webmanifest";
export const INSTALL_ICONS = Object.freeze([
  { path: "/icons/openbear-192.png", size: 192 },
  { path: "/icons/openbear-512.png", size: 512 },
  { path: "/icons/apple-touch-icon.png", size: 180 },
]);

export function httpsOrigin(input) {
  const text = String(input || "").trim();
  if (!/^https:\/\//i.test(text) || /[\s\\\u0000-\u001f\u007f]/.test(text)) {
    throw new Error("请输入完整的 HTTPS 地址，例如 https://bear.example.com");
  }
  let url;
  try { url = new URL(text); } catch { throw new Error("HTTPS 地址格式无效"); }
  if (url.protocol !== "https:" || !url.hostname || url.username || url.password || /^https:\/\/[^/]*@/i.test(text)) {
    throw new Error("地址不能包含用户名或密码");
  }
  // Deliberately discard path, query and fragment; never fetch this user input.
  return url.origin;
}

export function installationEnvironment(win) {
  const nav = win?.navigator || {};
  const ua = nav.userAgent || "";
  return {
    origin: win?.location?.origin || "",
    http: win?.location?.protocol === "http:",
    secure: win?.isSecureContext === true,
    standalone: nav.standalone === true || win?.matchMedia?.("(display-mode: standalone)")?.matches === true,
    ios: /iPad|iPhone|iPod/i.test(ua) || (/Macintosh/i.test(ua) && nav.maxTouchPoints > 1),
    embedded: /MicroMessenger|\bQQ\/|FBAN|FBAV|Instagram|Line\/|; wv\)/i.test(ua),
  };
}

function rootRelative(value, origin, base = `${origin}${MANIFEST_PATH}`) {
  if (typeof value !== "string" || !value || /^(?:[a-z][a-z\d+.-]*:|\/\/)/i.test(value)) return false;
  const url = new URL(value, base);
  return url.origin === origin && url.pathname === "/" && !url.search && !url.hash;
}

function validPng(bytes, size) {
  const signature = [137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82];
  if (bytes.length < 45 || !signature.every((value, i) => bytes[i] === value)) return false;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (view.getUint32(16) !== size || view.getUint32(20) !== size) return false;
  let hasPixels = false;
  for (let offset = 8; offset + 12 <= bytes.length;) {
    const length = view.getUint32(offset);
    const end = offset + 8 + length;
    if (end + 4 > bytes.length) return false;
    // Check complete chunks, not merely an IHDR that could be a truncated image.
    let crc = 0xffffffff;
    for (let i = offset + 4; i < end; i++) {
      crc ^= bytes[i];
      for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
    }
    if (((crc ^ 0xffffffff) >>> 0) !== view.getUint32(end)) return false;
    const type = view.getUint32(offset + 4);
    if (type === 0x49444154 && length > 0) hasPixels = true; // IDAT
    if (type === 0x49454e44) return hasPixels && length === 0 && end + 4 === bytes.length; // IEND
    offset = end + 4;
  }
  return false;
}

export async function checkInstallResources(win, { timeoutMs = 8000 } = {}) {
  const origin = win.location.origin;
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), timeoutMs);
  const get = async (path, type) => {
    const response = await win.fetch(`${origin}${path}`, {
      credentials: "omit", cache: "no-store", redirect: "error", referrerPolicy: "no-referrer", signal: abort.signal,
    });
    if (!response.ok || response.redirected || !type.test(response.headers.get("content-type") || "")) {
      throw new Error("安装资源不可用，请检查当前入口的静态资源与反向代理配置。");
    }
    return response;
  };
  try {
    const response = await get(MANIFEST_PATH, /^application\/(?:manifest\+json|json)(?:;|$)/i);
    const manifest = await response.json();
    if (!manifest || ![manifest.id, manifest.start_url, manifest.scope].every((value) => rootRelative(value, origin)) || manifest.display !== "standalone") {
      throw new Error("安装清单无效：启动地址与作用域必须属于当前站点，且不绑定会话。");
    }
    for (const { path, size } of INSTALL_ICONS.slice(0, 2)) {
      if (!Array.isArray(manifest.icons) || !manifest.icons.some((icon) => {
        if (typeof icon?.src !== "string" || /^(?:[a-z][a-z\d+.-]*:|\/\/)/i.test(icon.src)) return false;
        const url = new URL(icon.src, `${origin}${MANIFEST_PATH}`);
        return url.href === `${origin}${path}` && icon.sizes === `${size}x${size}` && icon.type === "image/png";
      })) throw new Error("安装清单缺少所需的同源 PNG 图标。");
    }
    await Promise.all(INSTALL_ICONS.map(async ({ path, size }) => {
      const icon = await get(path, /^image\/png(?:;|$)/i);
      const bytes = new Uint8Array(await icon.arrayBuffer());
      if (!validPng(bytes, size)) {
        throw new Error("安装图标格式或尺寸不正确。");
      }
    }));
  } finally {
    clearTimeout(timer);
    abort.abort();
  }
}

export function installPresentation(state) {
  if (state.standalone) return { kind: "standalone", title: "正在独立窗口中运行", detail: "这是当前窗口的显示模式，不代表能够查询设备上是否已安装其他副本。" };
  if (state.installedEvent) return { kind: "installed", title: "浏览器已报告安装完成", detail: "可以从桌面或主屏幕打开 OpenBear。当前页面不会自动跳转或刷新。" };
  if (!state.secure) return { kind: "insecure", title: "当前不是安全上下文", detail: "通常需要从你自己的可信 HTTPS 入口打开。本机 localhost 可能属于浏览器的安全上下文例外。" };
  if (state.resources === "error") return { kind: "resource-error", title: "安装资源检查未通过", detail: state.resourceError };
  if (state.resources !== "ready") return { kind: "checking", title: "检查当前站点的安装资源", detail: "只检查当前 origin 的清单与图标，不连接其他服务器。" };
  if (state.promptState === "prompting") return { kind: "prompting", title: "请在浏览器提示中选择", detail: "你可以取消安装；当前页面不会自动离开。" };
  if (state.promptState === "accepted") return { kind: "accepted", title: "已接受安装请求", detail: "请留意浏览器或系统的后续提示；接受请求不等于已完成安装。" };
  if (state.canPrompt) return { kind: "available", title: "浏览器提供了安装入口", detail: "点击下方按钮后才会请求系统安装提示，你仍可取消。" };
  if (state.embedded) return { kind: "embedded", title: "请在系统浏览器中打开", detail: "当前浏览器可能是应用内置浏览器。请用菜单在 Safari、Chrome 或 Edge 中打开你自己的入口，再检查安装方式。" };
  if (state.ios) return { kind: "manual-ios", title: "从分享菜单添加到主屏幕", detail: "在 Safari 中打开当前入口，点“分享”→“添加到主屏幕”；若有“作为网页 App 打开”，请保持开启。菜单可用性取决于系统与浏览器版本。" };
  const prefix = state.promptState === "dismissed" ? "你已取消安装。" : state.promptState === "expired" ? "上次安装提示已失效。" : "";
  return { kind: "manual", title: "当前没有可调用的系统安装提示", detail: `${prefix}可查看浏览器菜单中的“安装应用”“添加到主屏幕”，或 Safari 的“添加到程序坞”。安全上下文和资源可用不保证浏览器会提供安装；若没有此选项，可继续使用网页版。网页无法可靠判断设备是否已安装。` };
}

export function createInstallController(win, { checkResources = checkInstallResources } = {}) {
  let state = { ...installationEnvironment(win), resources: "idle", resourceError: "", canPrompt: false, promptState: "idle", installedEvent: false };
  let deferred = null;
  let attempt = 0;
  let checking = null;
  const listeners = new Set();
  const publish = (patch) => {
    state = { ...state, ...patch };
    for (const listener of listeners) listener({ ...state });
  };
  const clearPrompt = () => { deferred = null; attempt += 1; };
  const onPrompt = (event) => {
    if (typeof event.prompt !== "function" || state.standalone || state.installedEvent) return;
    event.preventDefault();
    clearPrompt();
    deferred = event;
    publish({ canPrompt: true, promptState: "available" });
  };
  const onInstalled = () => {
    clearPrompt();
    publish({ installedEvent: true, canPrompt: false, promptState: "idle" });
  };
  const refreshEnvironment = () => {
    const environment = installationEnvironment(win);
    if (environment.standalone) clearPrompt();
    publish({ ...environment, ...(environment.standalone ? { canPrompt: false, promptState: "idle" } : {}) });
  };
  const mode = win?.matchMedia?.("(display-mode: standalone)");
  // Registered at module startup by bootstrap.js, never delayed until Settings mounts.
  win?.addEventListener("beforeinstallprompt", onPrompt);
  win?.addEventListener("appinstalled", onInstalled);
  win?.addEventListener("pageshow", refreshEnvironment);
  if (mode?.addEventListener) mode.addEventListener("change", refreshEnvironment);
  else mode?.addListener?.(refreshEnvironment);

  return {
    getState: () => ({ ...state }),
    subscribe(listener) {
      listeners.add(listener);
      listener({ ...state });
      return () => listeners.delete(listener);
    },
    check() {
      refreshEnvironment();
      if (checking) return checking;
      publish({ resources: "checking", resourceError: "" });
      checking = Promise.resolve().then(() => checkResources(win)).then(() => {
        publish({ resources: "ready", resourceError: "" });
        return true;
      }, (error) => {
        publish({ resources: "error", resourceError: error?.name === "AbortError" ? "检查超时，请确认网络后重试。" : (error?.message || "安装资源无法获取，请稍后重试。") });
        return false;
      }).finally(() => { checking = null; });
      return checking;
    },
    async promptInstall({ userGesture = false } = {}) {
      if (!userGesture || !deferred || state.promptState === "prompting" || !state.secure || state.resources !== "ready" || state.standalone || state.installedEvent) return false;
      const event = deferred;
      deferred = null; // A BeforeInstallPromptEvent is single-use, including dismissal/failure.
      const token = ++attempt;
      publish({ canPrompt: false, promptState: "prompting" });
      try {
        // Do not await a resource check here: prompt() must retain the click's activation.
        const result = await event.prompt();
        const choice = await (event.userChoice || result);
        if (token === attempt) publish({ promptState: choice?.outcome === "accepted" ? "accepted" : choice?.outcome === "dismissed" ? "dismissed" : "expired" });
        return choice?.outcome === "accepted";
      } catch {
        if (token === attempt) publish({ promptState: "expired" });
        return false;
      }
    },
    destroy() {
      clearPrompt();
      listeners.clear();
      win?.removeEventListener("beforeinstallprompt", onPrompt);
      win?.removeEventListener("appinstalled", onInstalled);
      win?.removeEventListener("pageshow", refreshEnvironment);
      if (mode?.removeEventListener) mode.removeEventListener("change", refreshEnvironment);
      else mode?.removeListener?.(refreshEnvironment);
    },
  };
}
