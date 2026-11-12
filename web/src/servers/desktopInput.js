// PS/2 Set 1 scancodes for IronRDP and X11 keysyms for noVNC.
export const RDP_SCANCODES = {
  Escape: 0x01, Digit1: 0x02, Digit2: 0x03, Digit3: 0x04, Digit4: 0x05, Digit5: 0x06,
  Digit6: 0x07, Digit7: 0x08, Digit8: 0x09, Digit9: 0x0A, Digit0: 0x0B, Minus: 0x0C,
  Equal: 0x0D, Backspace: 0x0E, Tab: 0x0F,
  KeyQ: 0x10, KeyW: 0x11, KeyE: 0x12, KeyR: 0x13, KeyT: 0x14, KeyY: 0x15, KeyU: 0x16,
  KeyI: 0x17, KeyO: 0x18, KeyP: 0x19, BracketLeft: 0x1A, BracketRight: 0x1B, Enter: 0x1C,
  ControlLeft: 0x1D, KeyA: 0x1E, KeyS: 0x1F, KeyD: 0x20, KeyF: 0x21, KeyG: 0x22,
  KeyH: 0x23, KeyJ: 0x24, KeyK: 0x25, KeyL: 0x26, Semicolon: 0x27, Quote: 0x28,
  Backquote: 0x29, ShiftLeft: 0x2A, Backslash: 0x2B, KeyZ: 0x2C, KeyX: 0x2D, KeyC: 0x2E,
  KeyV: 0x2F, KeyB: 0x30, KeyN: 0x31, KeyM: 0x32, Comma: 0x33, Period: 0x34, Slash: 0x35,
  ShiftRight: 0x36, NumpadMultiply: 0x37, AltLeft: 0x38, Space: 0x39, CapsLock: 0x3A,
  F1: 0x3B, F2: 0x3C, F3: 0x3D, F4: 0x3E, F5: 0x3F, F6: 0x40, F7: 0x41, F8: 0x42,
  F9: 0x43, F10: 0x44, NumLock: 0x45, ScrollLock: 0x46,
  Numpad7: 0x47, Numpad8: 0x48, Numpad9: 0x49, NumpadSubtract: 0x4A,
  Numpad4: 0x4B, Numpad5: 0x4C, Numpad6: 0x4D, NumpadAdd: 0x4E,
  Numpad1: 0x4F, Numpad2: 0x50, Numpad3: 0x51, Numpad0: 0x52, NumpadDecimal: 0x53,
  F11: 0x57, F12: 0x58,
  NumpadEnter: 0xE01C, ControlRight: 0xE01D, NumpadDivide: 0xE035, PrintScreen: 0xE037,
  AltRight: 0xE038, Home: 0xE047, ArrowUp: 0xE048, PageUp: 0xE049, ArrowLeft: 0xE04B,
  ArrowRight: 0xE04D, End: 0xE04F, ArrowDown: 0xE050, PageDown: 0xE051, Insert: 0xE052,
  Delete: 0xE053, MetaLeft: 0xE05B, MetaRight: 0xE05C, ContextMenu: 0xE05D,
};

export const RDP_COMBOS = {
  cad: [0x1D, 0x38, 0xE053],        // Ctrl + Alt + Delete
  win: [0xE05B],                    // Windows key
  alttab: [0x38, 0x0F],             // Alt + Tab
  altf4: [0x38, 0x3E],              // Alt + F4
  taskmgr: [0x1D, 0x2A, 0x01],      // Ctrl + Shift + Esc
  lock: [0xE05B, 0x26],             // Win + L
  esc: [0x01],
  tab: [0x0F],
  up: [0xE048],
  down: [0xE050],
  left: [0xE04B],
  right: [0xE04D],
  enter: [0x1C],
  backspace: [0x0E],
};

// X11 keysyms for noVNC (macOS Screen Sharing)
export const VNC_KEYSYMS = {
  Escape: [0xFF1B, 'Escape'],
  Tab: [0xFF09, 'Tab'],
  Enter: [0xFF0D, 'Enter'],
  Backspace: [0xFF08, 'Backspace'],
  Up: [0xFF52, 'ArrowUp'],
  Down: [0xFF54, 'ArrowDown'],
  Left: [0xFF51, 'ArrowLeft'],
  Right: [0xFF53, 'ArrowRight'],
  ControlLeft: [0xFFE3, 'ControlLeft'],
  AltLeft: [0xFFE9, 'AltLeft'],
  MetaLeft: [0xFFE7, 'MetaLeft'],
  ShiftLeft: [0xFFE1, 'ShiftLeft'],
  Space: [0x0020, 'Space'],
  KeyQ: [0x0071, 'KeyQ'],
};

export const VNC_COMBOS = {
  win: [[0xFFE7, 'MetaLeft']],
  spotlight: [[0xFFE7, 'MetaLeft'], [0x0020, 'Space']],
  forcequit: [[0xFFE7, 'MetaLeft'], [0xFFE9, 'AltLeft'], [0xFF1B, 'Escape']],
  lock: [[0xFFE3, 'ControlLeft'], [0xFFE7, 'MetaLeft'], [0x0071, 'KeyQ']],
  alttab: [[0xFFE7, 'MetaLeft'], [0xFF09, 'Tab']],
  esc: [[0xFF1B, 'Escape']],
  tab: [[0xFF09, 'Tab']],
  up: [[0xFF52, 'ArrowUp']],
  down: [[0xFF54, 'ArrowDown']],
  left: [[0xFF51, 'ArrowLeft']],
  right: [[0xFF53, 'ArrowRight']],
  enter: [[0xFF0D, 'Enter']],
  backspace: [[0xFF08, 'Backspace']],
};

const SHIFTED_SYMBOLS = new Set('~!@#$%^&*()_+{}|:"<>?');

export function vncCharKey(ch) {
  if (ch === '\n' || ch === '\r') return {keysym: 0xFF0D, code: 'Enter', shift: false};
  if (ch === '\t') return {keysym: 0xFF09, code: 'Tab', shift: false};
  const cp = ch.codePointAt(0);
  if (!cp) return null;
  const isUpper = cp >= 0x41 && cp <= 0x5A;
  const shift = isUpper || SHIFTED_SYMBOLS.has(ch);
  const keysym = cp >= 0x20 && cp <= 0x7E ? cp : (0x01000000 | cp);
  return {keysym, code: null, shift};
}

export function charToKeyCode(ch) {
  const upper = ch.toUpperCase();
  if (upper >= 'A' && upper <= 'Z') return `Key${upper}`;
  if (upper >= '0' && upper <= '9') return `Digit${upper}`;
  return null;
}

export function formatIronError(err) {
  if (!err) return '连接已断开';
  try {
    if (typeof err.kind === 'function') {
      const k = err.kind();
      if (k === 1) return 'wrong_password';
      if (k === 2) return 'logon_failure';
      if (k === 3) return 'access_denied';
      if (typeof err.rdcleanpathDetails === 'function') {
        const d = err.rdcleanpathDetails();
        if (d) {
          if (d.wsaErrorCode === 10061) return 'connect_failed';
          if (d.wsaErrorCode === 10060 || d.httpStatusCode === 504) return 'tls_timeout';
          if (d.httpStatusCode === 503) return 'node_offline';
        }
      }
    }
    if (typeof err.backtrace === 'function') {
      const msg = String(err.backtrace() || '');
      if (/LogonFailure|WrongPassword|STATUS_LOGON_FAILURE/i.test(msg)) return 'logon_failure';
      if (/AccessDenied|STATUS_ACCESS_DENIED/i.test(msg)) return 'access_denied';
      if (/connect_failed|10061/i.test(msg)) return 'connect_failed';
      return msg.split('\n')[0].slice(0, 120) || '连接失败';
    }
  } catch { /* ignore */ }
  return err?.message || String(err);
}
