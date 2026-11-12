<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { Api } from '../api.js';
import { ok } from '../instances/instanceHelpers.js';
import { desktopReason, noAutofill, safeError } from './serverHelpers.js';
import { charToKeyCode, formatIronError, RDP_COMBOS, RDP_SCANCODES, VNC_COMBOS, vncCharKey } from './desktopInput.js';

// Lazy module loaders: neither @novnc/novnc nor ironrdp-wasm is fetched until
// the user actually opens a macOS or Windows desktop session. A load that
// failed is not kept, so the next connect tries again.
let vncModulePromise = null;
let rdpModulePromise = null;

function loadNoVnc() {
  return (vncModulePromise ||= import('@novnc/novnc').catch(err => {
    vncModulePromise = null;
    throw err;
  }));
}

function loadIronRdp() {
  return (rdpModulePromise ||= import('ironrdp-wasm').then(async mod => {
    await mod.default();
    try { mod.setup('warn'); } catch { /* ignore */ }
    return mod;
  }).catch(err => {
    rdpModulePromise = null;
    throw err;
  }));
}

const props = defineProps({
  serverId: {type: String, required: true},
  platform: {type: String, default: 'windows'},
  serverName: {type: String, default: ''},
  active: Boolean,
  phone: Boolean,
});
const emit = defineEmits(['status', 'meta']);

const SENTINEL = ' ';
const isMac = computed(() => props.platform === 'darwin');
const stage = ref(null), vncMount = ref(null), rdpCanvas = ref(null), mobileInput = ref(null);
const state = ref('connecting'), reason = ref(''), loadingText = ref('正在准备远程桌面…');
// 'width' fills the available width (great for 1080p macOS so it isn't tiny),
// 'fit' fits both width and height, 'original' is 100% 1:1, 'custom' uses zoomPct.
const scaleMode = ref(props.platform === 'darwin' ? 'width' : 'fit');
const zoomPct = ref(100);
// On phones, trackpad mode is much more accurate for small desktop targets; users can toggle anytime.
const touchMode = ref(props.phone ? 'trackpad' : 'direct'); // 'direct' | 'trackpad'
const keyboardOpen = ref(false), ctrlArmed = ref(false), altArmed = ref(false), isFullscreen = ref(false);
const presetResolution = ref('auto'); // 'auto' | '1280x720' | '1440x900' | '1920x1080'
const remoteSize = reactive({width: 1280, height: 720});
const canvasBox = reactive({width: '100%', height: '100%'});
const pointer = reactive({x: 640, y: 360});
const cursorPos = reactive({x: 0, y: 0, visible: false});

const authVisible = ref(false), settingsVisible = ref(false), clipboardVisible = ref(false), savingConfig = ref(false);
const clipboardText = ref(''), remoteClipboard = ref('');
const configLoaded = ref(false);
const form = reactive({
  port: '',
  defaultPort: props.platform === 'darwin' ? 5900 : 3389,
  username: '',
  password: '',
  domain: '',
  remember: true,
  source: 'none',
  hasSaved: false,
});

const protocolLabel = computed(() => isMac.value ? 'VNC 屏幕共享' : 'RDP 远程桌面');

let rfb = null, rfbClass = null, rdpMod = null, rdpSession = null, pendingVncCreds = false;
let observer = null, disposed = false, connectGen = 0, inputRaf = 0, pendingMove = null, pendingWheelV = 0, pendingWheelH = 0;
let isComposing = false, lastTapTime = 0, tapDragActive = false;

function emitMeta() {
  emit('meta', {
    scaleMode: scaleMode.value,
    zoomPct: zoomPct.value,
    touchMode: touchMode.value,
    keyboardOpen: keyboardOpen.value,
    ctrlArmed: ctrlArmed.value,
    altArmed: altArmed.value,
    isFullscreen: isFullscreen.value,
    protocol: isMac.value ? 'vnc' : 'rdp',
    resolution: `${remoteSize.width}×${remoteSize.height}`,
  });
}

function setState(value, why = '') {
  state.value = value;
  reason.value = why;
  emit('status', value === 'auth' ? 'connecting' : value, why);
  emitMeta();
}

function activeSurface() {
  return isMac.value ? vncMount.value?.querySelector('canvas') : rdpCanvas.value;
}

function scrollContainer() {
  if (isMac.value && rfb?._screen) return rfb._screen;
  return stage.value;
}

function updateVirtualCursorPosition() {
  const stageEl = stage.value, surface = activeSurface();
  if (!stageEl || !surface || state.value !== 'open') {
    cursorPos.visible = false;
    return;
  }
  const sRect = stageEl.getBoundingClientRect();
  const cRect = surface.getBoundingClientRect();
  if (!cRect.width || !cRect.height) {
    cursorPos.visible = false;
    return;
  }
  const rw = remoteSize.width || 1280, rh = remoteSize.height || 720;
  cursorPos.x = Math.round(cRect.left - sRect.left + (pointer.x / rw) * cRect.width);
  cursorPos.y = Math.round(cRect.top - sRect.top + (pointer.y / rh) * cRect.height);
  cursorPos.visible = Boolean(props.phone || touchMode.value === 'trackpad');
}

function ensureVncCursorVisible(instance) {
  if (!instance?._canvas) return;
  const cur = instance._canvas.style.cursor;
  if (!cur || cur === 'none') {
    instance._canvas.style.cursor = 'default';
  }
}

function updateCanvasScale() {
  const el = stage.value;
  if (!el) return;
  const cw = el.clientWidth || 1280, ch = el.clientHeight || 720;
  if (isMac.value) {
    if (!rfb || !rfb._display) return;
    const fw = rfb._fbWidth || remoteSize.width || 1920;
    const fh = rfb._fbHeight || remoteSize.height || 1080;
    remoteSize.width = fw;
    remoteSize.height = fh;
    rfb.clipViewport = false;
    let factor = 1.0;
    if (scaleMode.value === 'fit') {
      factor = Math.min(cw / fw, ch / fh);
      rfb._display.scale = factor;
      zoomPct.value = Math.max(10, Math.round(factor * 100));
    } else if (scaleMode.value === 'width') {
      factor = Math.max(0.2, cw / fw);
      if (fh * factor > ch + 2 && cw > 32) {
        factor = Math.max(0.2, (cw - 16) / fw);
      }
      rfb._display.scale = factor;
      zoomPct.value = Math.round(factor * 100);
    } else if (scaleMode.value === 'original') {
      factor = 1.0;
      rfb._display.scale = 1.0;
      zoomPct.value = 100;
    } else {
      factor = Math.max(0.25, Math.min(2.5, zoomPct.value / 100));
      rfb._display.scale = factor;
    }
    const tall = fh * factor > ch + 2;
    const wide = fw * factor > cw + 2;
    if (rfb._screen) {
      rfb._screen.style.overflow = (tall || wide) ? 'auto' : 'hidden';
      rfb._screen.style.alignItems = tall ? 'flex-start' : 'center';
      rfb._screen.style.justifyContent = wide ? 'flex-start' : 'center';
    }
    if (rfb._canvas) {
      rfb._canvas.style.margin = `${tall ? '0' : 'auto'} ${wide ? '0' : 'auto'}`;
    }
    ensureVncCursorVisible(rfb);
    updateVirtualCursorPosition();
    emitMeta();
    return;
  }
  const rw = remoteSize.width || 1280, rh = remoteSize.height || 720;
  let factor = 1.0;
  if (scaleMode.value === 'fit') {
    factor = Math.min(cw / rw, ch / rh);
    zoomPct.value = Math.max(10, Math.round(factor * 100));
  } else if (scaleMode.value === 'width') {
    factor = Math.max(0.2, cw / rw);
    if (rh * factor > ch + 2 && cw > 32) {
      factor = Math.max(0.2, (cw - 16) / rw);
    }
    zoomPct.value = Math.round(factor * 100);
  } else if (scaleMode.value === 'original') {
    factor = 1.0;
    zoomPct.value = 100;
  } else {
    factor = Math.max(0.25, Math.min(2.5, zoomPct.value / 100));
  }
  const scaledW = Math.max(1, Math.floor(rw * factor));
  const scaledH = Math.max(1, Math.floor(rh * factor));
  canvasBox.width = `${scaledW}px`;
  canvasBox.height = `${scaledH}px`;
  nextTick(updateVirtualCursorPosition);
  emitMeta();
}

function computeTargetResolution() {
  if (presetResolution.value !== 'auto') {
    const [w, h] = presetResolution.value.split('x').map(Number);
    if (w && h) return {width: w, height: h};
  }
  if (props.phone) return {width: 1280, height: 720};
  const el = stage.value;
  const cw = el?.clientWidth || 1280, ch = el?.clientHeight || 768;
  const align4 = n => Math.max(4, Math.floor(n / 4) * 4);
  return {
    width: align4(Math.min(1920, Math.max(1024, cw))),
    height: align4(Math.min(1080, Math.max(720, ch))),
  };
}

async function fetchConfig() {
  try {
    const cfg = ok(await Api.serverDesktopConfig(props.serverId));
    form.defaultPort = cfg.defaultPort || (isMac.value ? 5900 : 3389);
    form.port = cfg.customPort ? String(cfg.customPort) : '';
    form.username = cfg.username || (isMac.value ? '' : 'Administrator');
    form.password = cfg.password || '';
    form.domain = cfg.domain || '';
    form.source = cfg.source || 'none';
    form.hasSaved = Boolean(cfg.hasSaved);
    configLoaded.value = true;
  } catch (e) {
    configLoaded.value = true;
    throw e;
  }
}

function cleanupSession() {
  pendingVncCreds = false;
  if (inputRaf) {
    cancelAnimationFrame(inputRaf);
    inputRaf = 0;
  }
  pendingMove = null;
  pendingWheelV = 0;
  pendingWheelH = 0;
  if (rfb) {
    try { rfb.disconnect(); } catch { /* ignore */ }
    rfb = null;
  }
  if (vncMount.value) vncMount.value.replaceChildren();
  if (rdpSession) {
    const s = rdpSession;
    rdpSession = null;
    try { s.shutdown(); } catch { /* ignore */ }
  }
}

function wsUrlFor(kind) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const portParam = form.port && Number(form.port) > 0 ? `?port=${encodeURIComponent(form.port)}` : '';
  return `${proto}//${location.host}/api/servers/${encodeURIComponent(props.serverId)}/desktop/${kind}${portParam}`;
}

async function connect(withPromptIfMissing = true) {
  const gen = ++connectGen;
  cleanupSession();
  setState('connecting');
  try {
    if (!configLoaded.value) {
      loadingText.value = '正在读取桌面连接配置…';
      await fetchConfig();
    }
    if (disposed || gen !== connectGen) return;

    if (withPromptIfMissing && !form.password) {
      authVisible.value = true;
      setState('auth', '');
      return;
    }
    authVisible.value = false;

    if (isMac.value) {
      loadingText.value = '正在按需加载 VNC 客户端组件…';
      const {default: RFB} = await loadNoVnc();
      rfbClass = RFB;
      if (disposed || gen !== connectGen) return;
      loadingText.value = `正在连接 ${props.serverName || 'macOS'} 屏幕共享…`;
      await nextTick();
      startVncSession(RFB, gen);
    } else {
      loadingText.value = '正在按需加载 RDP 客户端组件 (WebAssembly)…';
      rdpMod = await loadIronRdp();
      if (disposed || gen !== connectGen) return;
      loadingText.value = `正在连接 ${props.serverName || 'Windows'} 远程桌面…`;
      await nextTick();
      await startRdpSession(rdpMod, gen);
    }
  } catch (e) {
    if (disposed || gen !== connectGen) return;
    const why = safeError(e);
    setState('closed', why);
  }
}

function startVncSession(RFB, gen) {
  if (!vncMount.value) return;
  vncMount.value.replaceChildren();
  const instance = new RFB(vncMount.value, wsUrlFor('vnc'), {
    credentials: {
      username: form.username || '',
      password: form.password || '',
    },
  });
  rfb = instance;
  instance.clipViewport = false;
  // Detach noVNC's built-in touch GestureHandler so our unified mobile trackpad/direct
  // touch engine handles both macOS VNC and Windows RDP consistently.
  try { instance._gestures?.detach?.(); } catch { /* ignore */ }
  instance._updateScale = () => {
    updateCanvasScale();
  };
  const origUpdateCursor = instance._updateCursor?.bind(instance);
  if (origUpdateCursor) {
    instance._updateCursor = (rgba, hotx, hoty, w, h) => {
      origUpdateCursor(rgba, hotx, hoty, w, h);
      ensureVncCursorVisible(instance);
    };
  }
  ensureVncCursorVisible(instance);

  let failReason = '';
  instance.addEventListener('connect', () => {
    if (disposed || gen !== connectGen) return;
    pendingVncCreds = false;
    remoteSize.width = instance._fbWidth || 1920;
    remoteSize.height = instance._fbHeight || 1080;
    pointer.x = Math.round(remoteSize.width / 2);
    pointer.y = Math.round(remoteSize.height / 2);
    setState('open');
    ensureVncCursorVisible(instance);
    nextTick(() => {
      requestAnimationFrame(() => {
        updateCanvasScale();
        ensureVncCursorVisible(instance);
        focus();
      });
    });
  });
  instance.addEventListener('credentialsrequired', e => {
    if (disposed || gen !== connectGen) return;
    const types = e.detail?.types || [];
    if (!pendingVncCreds && form.password && (!types.includes('username') || form.username)) {
      pendingVncCreds = true;
      instance.sendCredentials({username: form.username, password: form.password});
      return;
    }
    authVisible.value = true;
    setState('auth', types.includes('username') && !form.username ? 'macOS 屏幕共享需要系统用户名和密码' : '请输入屏幕共享密码');
  });
  instance.addEventListener('securityfailure', e => {
    if (disposed || gen !== connectGen) return;
    failReason = e.detail?.reason || 'auth_failed';
    authVisible.value = true;
  });
  instance.addEventListener('clipboard', e => {
    if (e.detail?.text) {
      remoteClipboard.value = e.detail.text;
      clipboardText.value = e.detail.text;
    }
  });
  instance.addEventListener('disconnect', e => {
    if (disposed || gen !== connectGen || rfb !== instance) return;
    rfb = null;
    if (failReason) {
      setState('closed', failReason);
      return;
    }
    setState('closed', e.detail?.clean ? 'closed' : 'connect_failed');
  });
}

async function startRdpSession(mod, gen) {
  const canvas = rdpCanvas.value;
  if (!canvas) return;
  const target = computeTargetResolution();
  remoteSize.width = target.width;
  remoteSize.height = target.height;
  canvas.width = target.width;
  canvas.height = target.height;
  pointer.x = Math.round(target.width / 2);
  pointer.y = Math.round(target.height / 2);
  updateCanvasScale();

  const builder = new mod.SessionBuilder();
  builder.username(form.username || 'Administrator');
  builder.password(form.password || '');
  if (form.domain) builder.serverDomain(form.domain);
  builder.destination(`127.0.0.1:${form.port || form.defaultPort || 3389}`);
  builder.proxyAddress(wsUrlFor('rdp'));
  builder.authToken('openbear');
  builder.desktopSize(new mod.DesktopSize(target.width, target.height));
  builder.renderCanvas(canvas);
  builder.extension(new mod.Extension('enable_credssp', true));

  builder.setCursorStyleCallbackContext(canvas);
  builder.setCursorStyleCallback(style => {
    if (canvas) canvas.style.cursor = style || 'default';
  });

  builder.canvasResizedCallback(() => {
    try {
      const ds = rdpSession?.desktopSize?.();
      if (ds) {
        if (ds.width && ds.height) {
          remoteSize.width = ds.width;
          remoteSize.height = ds.height;
        }
        ds.free?.();
      }
      updateCanvasScale();
    } catch { /* ignore */ }
  });

  builder.remoteClipboardChangedCallback(clipData => {
    try {
      if (!clipData || clipData.isEmpty()) return;
      const items = clipData.items() || [];
      for (const item of items) {
        if (item.mimeType() === 'text/plain') {
          const val = String(item.value() || '');
          if (val) {
            remoteClipboard.value = val;
            clipboardText.value = val;
            navigator.clipboard?.writeText?.(val).catch(() => {});
          }
        }
        item.free?.();
      }
    } catch { /* ignore */ } finally {
      try { clipData?.free?.(); } catch { /* ignore */ }
    }
  });

  builder.forceClipboardUpdateCallback(async () => {
    let data = null;
    try {
      data = new mod.ClipboardData();
      if (clipboardText.value) data.addText('text/plain', clipboardText.value);
      await rdpSession?.onClipboardPaste(data);
    } catch { /* ignore */ } finally {
      try { data?.free?.(); } catch { /* ignore */ }
    }
  });

  try {
    const session = await builder.connect();
    if (disposed || gen !== connectGen) {
      try { session.shutdown(); session.free?.(); } catch { /* ignore */ }
      return;
    }
    rdpSession = session;
    const ds = session.desktopSize();
    if (ds) {
      if (ds.width && ds.height) {
        remoteSize.width = ds.width;
        remoteSize.height = ds.height;
        canvas.width = ds.width;
        canvas.height = ds.height;
      }
      ds.free?.();
    }
    updateCanvasScale();
    setState('open');
    focus();

    session.run().then(info => {
      const why = typeof info?.reason === 'function' ? info.reason() : 'closed';
      try { info?.free?.(); } catch { /* ignore */ }
      try { session.free?.(); } catch { /* ignore */ }
      if (disposed || gen !== connectGen || rdpSession !== session) return;
      rdpSession = null;
      setState('closed', why || 'closed');
    }).catch(err => {
      const code = formatIronError(err);
      try { err?.free?.(); } catch { /* ignore */ }
      try { session.free?.(); } catch { /* ignore */ }
      if (disposed || gen !== connectGen || rdpSession !== session) return;
      rdpSession = null;
      if (code === 'wrong_password' || code === 'logon_failure' || code === 'access_denied') {
        authVisible.value = true;
      }
      setState('closed', code);
    });
  } catch (err) {
    const code = formatIronError(err);
    try { err?.free?.(); } catch { /* ignore */ }
    if (disposed || gen !== connectGen) return;
    if (code === 'wrong_password' || code === 'logon_failure' || code === 'access_denied') {
      authVisible.value = true;
    }
    setState('closed', code);
  }
}

// ----- Credentials & Settings Submit -----
async function submitAuth() {
  if (savingConfig.value) return;
  if (form.remember) {
    savingConfig.value = true;
    try {
      const cfg = ok(await Api.saveServerDesktopConfig(props.serverId, {
        port: form.port ? Number(form.port) : 0,
        username: form.username,
        password: form.password,
        domain: form.domain,
      }));
      form.hasSaved = Boolean(cfg.hasSaved);
      form.source = cfg.source || 'desktop';
    } catch (e) {
      ElMessage.error(safeError(e));
      savingConfig.value = false;
      return;
    }
    savingConfig.value = false;
  }
  settingsVisible.value = false;
  authVisible.value = false;
  if (isMac.value && rfb && pendingVncCreds) {
    rfb.sendCredentials({username: form.username, password: form.password});
    setState('open');
    return;
  }
  await connect(false);
}

async function resetSavedConfig() {
  if (savingConfig.value) return;
  savingConfig.value = true;
  try {
    await Api.saveServerDesktopConfig(props.serverId, {clear: true});
    configLoaded.value = false;
    await fetchConfig();
    ElMessage.success('已恢复默认端口与 SSH 凭据');
  } catch (e) {
    ElMessage.error(safeError(e));
  } finally {
    savingConfig.value = false;
  }
}

// ----- Unified Pointer & Wheel Dispatch (Both VNC & RDP) -----
function drainPendingRdpEvents() {
  if (inputRaf) {
    cancelAnimationFrame(inputRaf);
    inputRaf = 0;
  }
  const evs = [];
  if (pendingMove && rdpMod) {
    evs.push(rdpMod.DeviceEvent.mouseMove(pendingMove.x, pendingMove.y));
    pendingMove = null;
  }
  if (pendingWheelV !== 0 && rdpMod) {
    evs.push(rdpMod.DeviceEvent.wheelRotations(true, pendingWheelV, 1));
    pendingWheelV = 0;
  }
  if (pendingWheelH !== 0 && rdpMod) {
    evs.push(rdpMod.DeviceEvent.wheelRotations(false, pendingWheelH, 1));
    pendingWheelH = 0;
  }
  return evs;
}

function scheduleRdpInputFlush() {
  if (inputRaf) return;
  inputRaf = requestAnimationFrame(() => {
    inputRaf = 0;
    if (state.value !== 'open' || !rdpSession || !rdpMod) {
      pendingMove = null;
      pendingWheelV = 0;
      pendingWheelH = 0;
      return;
    }
    const evs = drainPendingRdpEvents();
    if (evs.length) applyRdpEvents(evs, false);
  });
}

function applyRdpEvents(events, flushPending = true) {
  if (!rdpSession || !rdpMod) return;
  try {
    const prefix = flushPending ? drainPendingRdpEvents() : [];
    if (!prefix.length && !events.length) return;
    const tx = new rdpMod.InputTransaction();
    for (const ev of prefix) tx.addEvent(ev);
    for (const ev of events) tx.addEvent(ev);
    rdpSession.applyInputs(tx);
  } catch { /* ignore */ }
}

function queueRdpMove(x, y) {
  pendingMove = {x, y};
  scheduleRdpInputFlush();
}

function queueRdpWheel(vertical, step) {
  if (vertical) pendingWheelV += step;
  else pendingWheelH += step;
  scheduleRdpInputFlush();
}

function sendVncPointer(x, y, mask) {
  if (!rfb || !rfb._sock || !rfbClass?.messages?.pointerEvent) return;
  try {
    rfbClass.messages.pointerEvent(rfb._sock, Math.round(x), Math.round(y), mask);
  } catch { /* ignore */ }
}

function dispatchPointerMove(x, y) {
  pointer.x = Math.max(0, Math.min((remoteSize.width || 1280) - 1, Math.round(x)));
  pointer.y = Math.max(0, Math.min((remoteSize.height || 720) - 1, Math.round(y)));
  updateVirtualCursorPosition();
  if (isMac.value) {
    sendVncPointer(pointer.x, pointer.y, tapDragActive ? 1 : 0);
  } else if (rdpSession && rdpMod) {
    queueRdpMove(pointer.x, pointer.y);
  }
}

function dispatchPointerButton(button, down) {
  if (isMac.value) {
    const mask = down ? (button === 2 ? 4 : button === 1 ? 2 : 1) : 0;
    sendVncPointer(pointer.x, pointer.y, mask);
  } else if (rdpSession && rdpMod) {
    const ev = down ? rdpMod.DeviceEvent.mouseButtonPressed(button) : rdpMod.DeviceEvent.mouseButtonReleased(button);
    applyRdpEvents([rdpMod.DeviceEvent.mouseMove(pointer.x, pointer.y), ev]);
  }
}

function dispatchPointerClick(button = 0) {
  dispatchPointerButton(button, true);
  dispatchPointerButton(button, false);
}

function dispatchWheel(vertical, step) {
  if (isMac.value) {
    const bit = vertical ? (step > 0 ? 8 : 16) : (step > 0 ? 32 : 64);
    sendVncPointer(pointer.x, pointer.y, bit);
    sendVncPointer(pointer.x, pointer.y, 0);
  } else if (rdpSession && rdpMod) {
    queueRdpWheel(vertical, step);
  }
}

// ----- Desktop Mouse & Keyboard Input (RDP) -----
function onRdpKeyDown(e) {
  if (state.value !== 'open' || !rdpSession || !rdpMod) return;
  if (authVisible.value || settingsVisible.value || clipboardVisible.value) return;
  const sc = RDP_SCANCODES[e.code];
  if (sc != null) {
    e.preventDefault();
    e.stopPropagation();
    applyRdpEvents([rdpMod.DeviceEvent.keyPressed(sc)]);
  }
}

function onRdpKeyUp(e) {
  if (state.value !== 'open' || !rdpSession || !rdpMod) return;
  if (authVisible.value || settingsVisible.value || clipboardVisible.value) return;
  const sc = RDP_SCANCODES[e.code];
  if (sc != null) {
    e.preventDefault();
    e.stopPropagation();
    applyRdpEvents([rdpMod.DeviceEvent.keyReleased(sc)]);
  }
}

function surfaceCoords(clientX, clientY) {
  const surface = activeSurface();
  if (!surface) return {x: pointer.x, y: pointer.y};
  const rect = surface.getBoundingClientRect();
  if (!rect.width || !rect.height) return {x: pointer.x, y: pointer.y};
  const rw = remoteSize.width || surface.width || 1280;
  const rh = remoteSize.height || surface.height || 720;
  const x = Math.max(0, Math.min(rw - 1, Math.round((clientX - rect.left) * (rw / rect.width))));
  const y = Math.max(0, Math.min(rh - 1, Math.round((clientY - rect.top) * (rh / rect.height))));
  pointer.x = x;
  pointer.y = y;
  updateVirtualCursorPosition();
  return {x, y};
}

function onRdpMouseMove(e) {
  if (state.value !== 'open' || !rdpSession || !rdpMod) return;
  const {x, y} = surfaceCoords(e.clientX, e.clientY);
  queueRdpMove(x, y);
}

function onRdpMouseDown(e) {
  if (state.value !== 'open' || !rdpSession || !rdpMod) return;
  e.preventDefault();
  rdpCanvas.value?.focus();
  const {x, y} = surfaceCoords(e.clientX, e.clientY);
  pendingMove = null;
  applyRdpEvents([
    rdpMod.DeviceEvent.mouseMove(x, y),
    rdpMod.DeviceEvent.mouseButtonPressed(e.button),
  ]);
}

function onRdpMouseUp(e) {
  if (state.value !== 'open' || !rdpSession || !rdpMod) return;
  e.preventDefault();
  const {x, y} = surfaceCoords(e.clientX, e.clientY);
  pendingMove = null;
  applyRdpEvents([
    rdpMod.DeviceEvent.mouseMove(x, y),
    rdpMod.DeviceEvent.mouseButtonReleased(e.button),
  ]);
}

function onRdpWheel(e) {
  if (state.value !== 'open' || !rdpSession || !rdpMod) return;
  e.preventDefault();
  if (e.deltaY !== 0) queueRdpWheel(true, e.deltaY > 0 ? -1 : 1);
  if (e.deltaX !== 0) queueRdpWheel(false, e.deltaX > 0 ? -1 : 1);
}

// ----- Unified Mobile Touch Engine (Trackpad & Direct for both macOS VNC & Windows RDP) -----
let touchState = null;

function autoPanViewportNearEdge() {
  const scroller = scrollContainer();
  const stageEl = stage.value;
  if (!scroller || !stageEl) return;
  const margin = 36, step = 14;
  const w = stageEl.clientWidth, h = stageEl.clientHeight;
  let dx = 0, dy = 0;
  if (cursorPos.x < margin) dx = -step;
  else if (cursorPos.x > w - margin) dx = step;
  if (cursorPos.y < margin) dy = -step;
  else if (cursorPos.y > h - margin) dy = step;
  if (dx || dy) {
    scroller.scrollBy?.({left: dx, top: dy});
  }
}

function onStageTouchStart(e) {
  if (state.value !== 'open') return;
  if (authVisible.value || settingsVisible.value || clipboardVisible.value) return;

  if (e.touches.length === 1) {
    const t = e.touches[0];
    const now = Date.now();
    const isDoubleTap = touchMode.value === 'trackpad' && (now - lastTapTime < 320);
    if (isDoubleTap) {
      tapDragActive = true;
      dispatchPointerButton(0, true);
    }
    const startPos = touchMode.value === 'direct' ? surfaceCoords(t.clientX, t.clientY) : {x: pointer.x, y: pointer.y};
    touchState = {
      count: 1,
      startX: t.clientX,
      startY: t.clientY,
      lastX: t.clientX,
      lastY: t.clientY,
      moved: false,
      dragging: tapDragActive,
      longPressed: false,
      timer: setTimeout(() => {
        if (touchState && !touchState.moved && !tapDragActive) {
          touchState.longPressed = true;
          if (touchMode.value === 'direct') {
            dispatchPointerMove(startPos.x, startPos.y);
          }
          dispatchPointerClick(2);
        }
      }, 480),
    };
  } else if (e.touches.length === 2) {
    if (touchState?.timer) clearTimeout(touchState.timer);
    if (tapDragActive) {
      tapDragActive = false;
      dispatchPointerButton(0, false);
    }
    touchState = {
      count: 2,
      lastX: (e.touches[0].clientX + e.touches[1].clientX) / 2,
      lastY: (e.touches[0].clientY + e.touches[1].clientY) / 2,
      moved: false,
    };
  }
}

function onStageTouchMove(e) {
  if (!touchState || state.value !== 'open') return;
  if (authVisible.value || settingsVisible.value || clipboardVisible.value) return;

  if (e.touches.length === 1 && touchState.count === 1) {
    const t = e.touches[0];
    const totalDx = t.clientX - touchState.startX;
    const totalDy = t.clientY - touchState.startY;
    if (Math.hypot(totalDx, totalDy) > 6) {
      touchState.moved = true;
      clearTimeout(touchState.timer);
    }
    if (!touchState.moved) return;

    if (touchMode.value === 'trackpad') {
      e.preventDefault();
      const stepX = t.clientX - touchState.lastX;
      const stepY = t.clientY - touchState.lastY;
      touchState.lastX = t.clientX;
      touchState.lastY = t.clientY;
      // Adaptive acceleration: fine precision on slow moves, fast traversal on quick swipes
      const speed = Math.hypot(stepX, stepY);
      const accel = speed > 18 ? 2.4 : speed > 8 ? 1.85 : 1.35;
      dispatchPointerMove(pointer.x + stepX * accel, pointer.y + stepY * accel);
      autoPanViewportNearEdge();
    } else {
      // Direct mode: if viewport is scrollable (width-fill or zoomed in), single-finger drag pans the viewport naturally;
      // if fitted to screen, single-finger drag drags on the remote desktop.
      const scroller = scrollContainer();
      const canPan = scroller && (scroller.scrollWidth > scroller.clientWidth + 4 || scroller.scrollHeight > scroller.clientHeight + 4);
      if (canPan) {
        e.preventDefault();
        const dx = touchState.lastX - t.clientX;
        const dy = touchState.lastY - t.clientY;
        touchState.lastX = t.clientX;
        touchState.lastY = t.clientY;
        scroller.scrollBy?.({left: dx, top: dy});
      } else {
        e.preventDefault();
        const pos = surfaceCoords(t.clientX, t.clientY);
        if (!touchState.dragging) {
          touchState.dragging = true;
          dispatchPointerButton(0, true);
        }
        dispatchPointerMove(pos.x, pos.y);
      }
    }
  } else if (e.touches.length === 2) {
    e.preventDefault();
    const midY = (e.touches[0].clientY + e.touches[1].clientY) / 2;
    const deltaY = midY - (touchState.lastY || midY);
    if (Math.abs(deltaY) >= 16) {
      touchState.moved = true;
      touchState.lastY = midY;
      dispatchWheel(true, deltaY > 0 ? 1 : -1);
    }
  }
}

function onStageTouchEnd(e) {
  if (!touchState || state.value !== 'open') return;
  clearTimeout(touchState.timer);
  if (touchState.count === 1 && !touchState.longPressed) {
    if (tapDragActive) {
      tapDragActive = false;
      dispatchPointerButton(0, false);
    } else if (touchState.dragging) {
      dispatchPointerButton(0, false);
    } else if (!touchState.moved) {
      lastTapTime = Date.now();
      dispatchPointerClick(0);
    }
  } else if (touchState.count === 2 && !touchState.moved) {
    dispatchPointerClick(2);
  }
  if (e.touches.length === 0) touchState = null;
}

// ----- Mobile Soft Keyboard (Sentinel + IME Composition + Modifier Latch) -----
function resetMobileSentinel() {
  const el = mobileInput.value;
  if (!el) return;
  el.value = SENTINEL;
  try { el.setSelectionRange(1, 1); } catch { /* ignore */ }
}

function sendModifiedChar(ch) {
  const code = charToKeyCode(ch);
  const useCtrl = ctrlArmed.value;
  const useAlt = altArmed.value;
  ctrlArmed.value = false;
  altArmed.value = false;
  emitMeta();

  if (isMac.value && rfb) {
    const mods = [];
    if (useCtrl) mods.push([0xFFE3, 'ControlLeft']);
    if (useAlt) mods.push([0xFFE9, 'AltLeft']);
    const vk = vncCharKey(ch.toLowerCase());
    if (!vk) return;
    for (const [ks, c] of mods) rfb.sendKey(ks, c, true);
    rfb.sendKey(vk.keysym, code, true);
    rfb.sendKey(vk.keysym, code, false);
    for (let i = mods.length - 1; i >= 0; i--) rfb.sendKey(mods[i][0], mods[i][1], false);
    return;
  }
  if (rdpSession && rdpMod && code && RDP_SCANCODES[code] != null) {
    const sc = RDP_SCANCODES[code];
    const events = [];
    if (useCtrl) events.push(rdpMod.DeviceEvent.keyPressed(RDP_SCANCODES.ControlLeft));
    if (useAlt) events.push(rdpMod.DeviceEvent.keyPressed(RDP_SCANCODES.AltLeft));
    events.push(rdpMod.DeviceEvent.keyPressed(sc));
    events.push(rdpMod.DeviceEvent.keyReleased(sc));
    if (useAlt) events.push(rdpMod.DeviceEvent.keyReleased(RDP_SCANCODES.AltLeft));
    if (useCtrl) events.push(rdpMod.DeviceEvent.keyReleased(RDP_SCANCODES.ControlLeft));
    applyRdpEvents(events);
  }
}

function sendTextToDesktop(text) {
  if (!text || state.value !== 'open') return;
  if ((ctrlArmed.value || altArmed.value) && text.length === 1) {
    sendModifiedChar(text);
    return;
  }
  if (isMac.value && rfb) {
    for (const ch of text) {
      const vk = vncCharKey(ch);
      if (!vk) continue;
      if (vk.shift) rfb.sendKey(0xFFE1, 'ShiftLeft', true);
      rfb.sendKey(vk.keysym, vk.code, true);
      rfb.sendKey(vk.keysym, vk.code, false);
      if (vk.shift) rfb.sendKey(0xFFE1, 'ShiftLeft', false);
    }
  } else if (rdpSession && rdpMod) {
    const events = [];
    for (const ch of text) {
      if (ch === '\n' || ch === '\r') {
        events.push(rdpMod.DeviceEvent.keyPressed(RDP_SCANCODES.Enter));
        events.push(rdpMod.DeviceEvent.keyReleased(RDP_SCANCODES.Enter));
      } else {
        events.push(rdpMod.DeviceEvent.unicodePressed(ch));
        events.push(rdpMod.DeviceEvent.unicodeReleased(ch));
      }
    }
    applyRdpEvents(events);
  }
}

function onMobileCompositionStart() {
  isComposing = true;
}

function onMobileCompositionEnd(e) {
  isComposing = false;
  const committed = e.data || (e.target.value.startsWith(SENTINEL) ? e.target.value.slice(SENTINEL.length) : e.target.value);
  if (committed) {
    sendTextToDesktop(committed);
  }
  resetMobileSentinel();
}

function onMobileTextInput(e) {
  if (isComposing || e.isComposing) return;
  const val = e.target.value;
  if (val === '') {
    // Sentinel character was deleted by mobile Backspace!
    sendCombo('backspace');
    resetMobileSentinel();
    return;
  }
  const added = val.startsWith(SENTINEL) ? val.slice(SENTINEL.length) : val;
  if (added) {
    sendTextToDesktop(added);
  }
  resetMobileSentinel();
}

function onMobileKeyDown(e) {
  if (isComposing || e.isComposing) return;
  if (e.key === 'Backspace') {
    e.preventDefault();
    sendCombo('backspace');
    resetMobileSentinel();
  } else if (e.key === 'Enter') {
    e.preventDefault();
    sendCombo('enter');
    resetMobileSentinel();
  } else if (e.key === 'Escape') {
    e.preventDefault();
    sendCombo('esc');
  } else if (e.key === 'Tab') {
    e.preventDefault();
    sendCombo('tab');
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    sendCombo('up');
  } else if (e.key === 'ArrowDown') {
    e.preventDefault();
    sendCombo('down');
  } else if (e.key === 'ArrowLeft') {
    e.preventDefault();
    sendCombo('left');
  } else if (e.key === 'ArrowRight') {
    e.preventDefault();
    sendCombo('right');
  }
}

function onMobileFocus() {
  keyboardOpen.value = true;
  resetMobileSentinel();
  emitMeta();
}

function onMobileBlur() {
  keyboardOpen.value = false;
  isComposing = false;
  emitMeta();
}

function toggleMobileKeyboard() {
  const el = mobileInput.value;
  if (!el) return;
  if (keyboardOpen.value && document.activeElement === el) {
    el.blur();
  } else {
    resetMobileSentinel();
    el.focus({preventScroll: true});
  }
}

function toggleModifier(mod) {
  if (mod === 'ctrl') ctrlArmed.value = !ctrlArmed.value;
  else if (mod === 'alt') altArmed.value = !altArmed.value;
  emitMeta();
}

function sendCombo(name) {
  if (state.value !== 'open') return;
  if (isMac.value && rfb) {
    if (name === 'cad') {
      rfb.sendCtrlAltDel();
      return;
    }
    const seq = VNC_COMBOS[name];
    if (!seq) return;
    for (const [ks, code] of seq) rfb.sendKey(ks, code, true);
    for (let i = seq.length - 1; i >= 0; i--) rfb.sendKey(seq[i][0], seq[i][1], false);
    return;
  }
  if (rdpSession && rdpMod) {
    const seq = RDP_COMBOS[name];
    if (!seq) return;
    const events = [];
    for (const sc of seq) events.push(rdpMod.DeviceEvent.keyPressed(sc));
    for (let i = seq.length - 1; i >= 0; i--) events.push(rdpMod.DeviceEvent.keyReleased(seq[i]));
    applyRdpEvents(events);
  }
}

function triggerRightClick() {
  if (state.value !== 'open') return;
  dispatchPointerClick(2);
}

function onStageMouseMove(e) {
  if (props.phone || state.value !== 'open' || scaleMode.value === 'fit') return;
  if (authVisible.value || settingsVisible.value || clipboardVisible.value) return;
  const scroller = scrollContainer();
  const stageEl = stage.value;
  if (!scroller || !stageEl) return;
  const canScrollY = scroller.scrollHeight > scroller.clientHeight + 4;
  const canScrollX = scroller.scrollWidth > scroller.clientWidth + 4;
  if (!canScrollY && !canScrollX) return;

  const rect = stageEl.getBoundingClientRect();
  const relX = e.clientX - rect.left;
  const relY = e.clientY - rect.top;
  const zone = 44;
  let dx = 0, dy = 0;
  if (canScrollY) {
    if (relY >= 0 && relY < zone && scroller.scrollTop > 0) {
      dy = -Math.ceil(((zone - relY) / zone) * 18);
    } else if (relY <= rect.height && relY > rect.height - zone && scroller.scrollTop + scroller.clientHeight < scroller.scrollHeight) {
      dy = Math.ceil(((relY - (rect.height - zone)) / zone) * 18);
    }
  }
  if (canScrollX) {
    if (relX >= 0 && relX < zone && scroller.scrollLeft > 0) {
      dx = -Math.ceil(((zone - relX) / zone) * 18);
    } else if (relX <= rect.width && relX > rect.width - zone && scroller.scrollLeft + scroller.clientWidth < scroller.scrollWidth) {
      dx = Math.ceil(((relX - (rect.width - zone)) / zone) * 18);
    }
  }
  if (dx || dy) {
    scroller.scrollBy({left: dx, top: dy});
  }
}

function onStageWheelCapture(e) {
  if (state.value !== 'open') return;
  if (e.altKey && e.deltaY !== 0) {
    e.preventDefault();
    e.stopPropagation();
    zoomBy(e.deltaY < 0 ? 10 : -10);
  }
}

function setScaleMode(mode) {
  if (mode.startsWith('pct:')) {
    const pct = Number(mode.slice(4));
    if (pct >= 25 && pct <= 250) {
      scaleMode.value = pct === 100 ? 'original' : 'custom';
      zoomPct.value = pct;
      updateCanvasScale();
    }
    return;
  }
  scaleMode.value = mode;
  updateCanvasScale();
}

function toggleScale() {
  const order = ['width', 'fit', 'original'];
  const next = order[(order.indexOf(scaleMode.value) + 1) % order.length];
  setScaleMode(next);
}

function zoomBy(delta) {
  scaleMode.value = 'custom';
  const base = Math.round(zoomPct.value / 10) * 10;
  const next = delta > 0
    ? (base <= zoomPct.value ? base + delta : base)
    : (base >= zoomPct.value ? base + delta : base);
  zoomPct.value = Math.min(250, Math.max(25, next));
  updateCanvasScale();
}

function toggleTouchMode() {
  touchMode.value = touchMode.value === 'direct' ? 'trackpad' : 'direct';
  updateVirtualCursorPosition();
  ElMessage.info(touchMode.value === 'trackpad' ? '触控板模式：单指滑动移动光标，轻点左键，双指轻点右键，双指滑动滚轮' : '直点模式：点按位置即左键，长按右键，单指滑动平移画面');
  emitMeta();
}

function matchResolution() {
  if (isMac.value) return;
  const target = computeTargetResolution();
  if (rdpSession && typeof rdpSession.resize === 'function') {
    try {
      rdpSession.resize(target.width, target.height);
      remoteSize.width = target.width;
      remoteSize.height = target.height;
      updateCanvasScale();
      return;
    } catch { /* fall back to reconnect */ }
  }
  void connect(false);
}

async function syncClipboardToRemote() {
  const text = clipboardText.value;
  if (!text) return;
  if (isMac.value && rfb) {
    rfb.clipboardPasteFrom(text);
    ElMessage.success('已同步到远程剪贴板');
    clipboardVisible.value = false;
  } else if (rdpSession && rdpMod) {
    let data = null;
    try {
      data = new rdpMod.ClipboardData();
      data.addText('text/plain', text);
      await rdpSession.onClipboardPaste(data);
      ElMessage.success('已同步到远程剪贴板');
      clipboardVisible.value = false;
    } catch {
      sendTextToDesktop(text);
      ElMessage.success('已直接输入到远程桌面');
      clipboardVisible.value = false;
    } finally {
      try { data?.free?.(); } catch { /* ignore */ }
    }
  }
}

function typeClipboardToRemote() {
  if (!clipboardText.value) return;
  sendTextToDesktop(clipboardText.value);
  clipboardVisible.value = false;
}

function toggleFullscreen() {
  const el = stage.value?.closest('.tw-main') || stage.value;
  if (!el) return;
  if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
  else el.requestFullscreen?.().catch(() => {});
}

function openClipboard() {
  clipboardVisible.value = true;
}

function openSettings() {
  settingsVisible.value = true;
}

function focus() {
  if (props.phone) return;
  if (isMac.value) {
    vncMount.value?.querySelector('canvas')?.focus();
  } else {
    rdpCanvas.value?.focus();
  }
}

function onWindowBlur() {
  if (rdpSession) {
    try { rdpSession.releaseAllInputs(); } catch { /* ignore */ }
  }
}

watch(() => props.phone, isPhone => {
  touchMode.value = isPhone ? 'trackpad' : 'direct';
  updateVirtualCursorPosition();
  emitMeta();
});

watch(() => props.active, v => {
  if (v) nextTick(() => { requestAnimationFrame(() => { updateCanvasScale(); focus(); }); });
});

function onFullscreenChange() {
  const hostEl = stage.value?.closest('.tw-main') || stage.value;
  isFullscreen.value = Boolean(document.fullscreenElement && (document.fullscreenElement === hostEl || hostEl?.contains(document.fullscreenElement)));
  emitMeta();
  nextTick(() => {
    requestAnimationFrame(() => {
      updateCanvasScale();
      if (props.active) focus();
    });
  });
}

onMounted(() => {
  observer = new ResizeObserver(() => updateCanvasScale());
  if (stage.value) observer.observe(stage.value);
  window.addEventListener('blur', onWindowBlur);
  document.addEventListener('fullscreenchange', onFullscreenChange);
  void connect(true);
});

onBeforeUnmount(() => {
  disposed = true;
  observer?.disconnect();
  window.removeEventListener('blur', onWindowBlur);
  document.removeEventListener('fullscreenchange', onFullscreenChange);
  cleanupSession();
});

defineExpose({
  reconnect: () => { configLoaded.value = false; return connect(true); },
  setScaleMode,
  toggleScale,
  zoomBy,
  toggleTouchMode,
  toggleModifier,
  matchResolution,
  toggleFullscreen,
  sendCombo,
  triggerRightClick,
  openClipboard,
  openSettings,
  openMobileKeyboard: toggleMobileKeyboard,
  focus,
});
</script>

<template>
  <div
    ref="stage"
    class="dp-stage"
    :class="{'is-scrollable': scaleMode !== 'fit', 'is-open': state === 'open'}"
    data-desktop-pane
    @touchstart="onStageTouchStart"
    @touchmove="onStageTouchMove"
    @touchend="onStageTouchEnd"
    @touchcancel="onStageTouchEnd"
    @mousemove="onStageMouseMove"
    @wheel.capture="onStageWheelCapture"
    @scroll="updateVirtualCursorPosition"
  >
    <!-- macOS VNC surface -->
    <div v-if="isMac" ref="vncMount" class="dp-vnc-mount" :class="{'is-hidden': state !== 'open'}" />

    <!-- Windows RDP canvas -->
    <canvas
      v-else
      ref="rdpCanvas"
      class="dp-rdp-canvas"
      :class="{'is-hidden': state !== 'open'}"
      :style="canvasBox"
      tabindex="0"
      aria-label="Windows 远程桌面画布"
      @keydown="onRdpKeyDown"
      @keyup="onRdpKeyUp"
      @mousemove="onRdpMouseMove"
      @mousedown="onRdpMouseDown"
      @mouseup="onRdpMouseUp"
      @wheel="onRdpWheel"
      @contextmenu.prevent
    />

    <!-- Virtual mouse cursor indicator for mobile / trackpad mode -->
    <div
      v-if="state === 'open' && cursorPos.visible"
      class="dp-virtual-cursor"
      :class="{'is-dragging': tapDragActive}"
      :style="{transform: `translate3d(${cursorPos.x}px, ${cursorPos.y}px, 0)`}"
      aria-hidden="true"
    >
      <svg viewBox="0 0 24 24">
        <path d="M4 2 L4 19 L8.5 14.8 L12.2 22 L14.8 20.7 L11.2 13.6 L17.5 13.6 Z" fill="#ffffff" stroke="#111827" stroke-width="1.8" stroke-linejoin="round" />
      </svg>
    </div>

    <!-- Mobile soft-keyboard input (kept inside viewport with sentinel so iOS/Android Backspace and IME work reliably) -->
    <input
      ref="mobileInput"
      v-bind="noAutofill"
      type="text"
      inputmode="text"
      autocapitalize="off"
       autocorrect="off"
      spellcheck="false"
      class="dp-mobile-input"
      aria-label="远程桌面软键盘输入"
      @focus="onMobileFocus"
      @blur="onMobileBlur"
      @compositionstart="onMobileCompositionStart"
      @compositionend="onMobileCompositionEnd"
      @input="onMobileTextInput"
      @keydown="onMobileKeyDown"
    />

    <!-- Loading state -->
    <div v-if="state === 'connecting'" class="dp-overlay" role="status">
      <div class="dp-card dp-status-card">
        <svg class="dp-spinner-svg" viewBox="0 0 24 24" aria-hidden="true">
          <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-opacity="0.2" stroke-width="2.5" />
          <path d="M12 3a9 9 0 0 1 9 9" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
            <animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="0.75s" repeatCount="indefinite" />
          </path>
        </svg>
        <strong>{{ loadingText }}</strong>
        <small class="instance-muted">{{ protocolLabel }} · 经节点安全隧道转发</small>
      </div>
    </div>

    <!-- Credentials / Settings Card (inline when auth needed or opened from toolbar) -->
    <div v-if="state === 'auth' || authVisible || settingsVisible" class="dp-overlay" role="dialog" :aria-label="`${serverName || ''} 桌面连接设置`">
      <form class="dp-card dp-form" autocomplete="off" @submit.prevent="submitAuth">
        <header class="dp-card-head">
          <div>
            <h3>{{ serverName || '远程桌面' }} · {{ protocolLabel }}</h3>
            <small class="instance-muted">
              {{ form.source === 'ssh' ? '已自动带入这台服务器的 SSH 账号密码' : form.hasSaved ? '使用已保存的桌面凭据' : '经节点连接本机远程桌面服务' }}
            </small>
          </div>
          <button v-if="settingsVisible && state === 'open'" type="button" class="ix-text-button" @click="settingsVisible = false">返回桌面</button>
        </header>

        <p v-if="reason" class="dp-alert" role="alert">{{ desktopReason(reason) }}</p>

        <div class="dp-fields">
          <label class="dp-field">
            <span>用户名</span>
            <input v-model="form.username" v-bind="noAutofill" type="text" :placeholder="isMac ? 'macOS 系统用户名（纯 VNC 密码可留空）' : '例如 Administrator'" />
          </label>
          <label class="dp-field">
            <span>密码</span>
            <input v-model="form.password" v-bind="noAutofill" type="password" required placeholder="输入远程桌面或系统登录密码" />
          </label>
          <div class="dp-field-row">
            <label class="dp-field">
              <span>本机端口</span>
              <input v-model="form.port" v-bind="noAutofill" type="number" min="1" max="65535" class="mono" :placeholder="`默认 ${form.defaultPort}`" />
            </label>
            <label v-if="!isMac" class="dp-field">
              <span>域（可选）</span>
              <input v-model="form.domain" v-bind="noAutofill" type="text" placeholder="留空为本地账户" />
            </label>
            <label v-if="!isMac" class="dp-field">
              <span>分辨率</span>
              <select v-model="presetResolution">
                <option value="auto">自适应窗口</option>
                <option value="1280x720">1280 × 720</option>
                <option value="1440x900">1440 × 900</option>
                <option value="1920x1080">1920 × 1080</option>
              </select>
            </label>
          </div>
        </div>

        <label class="ob-check dp-remember">
          <input v-model="form.remember" type="checkbox" />
          <span>记住桌面登录信息<small class="instance-muted">加密保存在主控，下次打开直接连接</small></span>
        </label>

        <footer class="dp-card-foot">
          <button v-if="form.hasSaved" type="button" class="ix-text-button" :disabled="savingConfig" @click="resetSavedConfig">恢复默认跟随 SSH</button>
          <span class="ob-grow" />
          <el-button v-if="settingsVisible && state === 'open'" @click="settingsVisible = false">取消</el-button>
          <el-button type="primary" native-type="submit" :loading="savingConfig">连接桌面</el-button>
        </footer>
      </form>
    </div>

    <!-- Disconnected state (when not showing auth form) -->
    <div v-else-if="state === 'closed'" class="dp-overlay" role="status">
      <div class="dp-card dp-status-card">
        <strong>{{ desktopReason(reason) }}</strong>
        <small class="instance-muted">可以重新连接，或修改桌面用户名、密码和端口设置。</small>
        <div class="dp-actions">
          <el-button @click="authVisible = true">连接设置</el-button>
          <el-button type="primary" @click="connect(true)">重新连接</el-button>
        </div>
      </div>
    </div>

    <!-- Clipboard modal -->
    <div v-if="clipboardVisible" class="dp-overlay" role="dialog" aria-label="远程桌面剪贴板">
      <div class="dp-card dp-form">
        <header class="dp-card-head">
          <div>
            <h3>剪贴板与文本输入</h3>
            <small class="instance-muted">在这里粘贴或查看远程桌面的文本，也可直接作为按键输入到当前焦点窗口</small>
          </div>
          <button type="button" class="ix-text-button" @click="clipboardVisible = false">关闭</button>
        </header>
        <textarea v-model="clipboardText" rows="5" class="dp-clip-area" placeholder="在此输入或粘贴要发往远程桌面的文字…" />
        <footer class="dp-card-foot">
          <el-button @click="typeClipboardToRemote">作为按键输入</el-button>
          <span class="ob-grow" />
          <el-button @click="clipboardVisible = false">取消</el-button>
          <el-button type="primary" @click="syncClipboardToRemote">同步到远程剪贴板</el-button>
        </footer>
      </div>
    </div>
  </div>
</template>
