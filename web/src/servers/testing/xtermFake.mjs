// A stand-in for @xterm/xterm and its addons in the happy-dom tests (xterm's renderer needs a
// real browser). It keeps what the page relies on: written output shows as plain text lines in
// .xterm-rows, keys typed in its textarea reach onData (Enter as \r, Ctrl+letter as a control
// character) after the custom key handler, and search finds text in the output. OSC 8 links are
// spans (.xterm-link): pointing at one calls options.linkHandler.hover / .leave, a click .activate,
// with the link's range in the buffer as xterm gives it (1-based columns and lines, end included).
// The real terminal (rendering, fonts, fitting) is covered by the browser tests.
const decoder = new TextDecoder();
// eslint-disable-next-line no-control-regex
const ANSI = /\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[()][0-9A-B]/g;
// eslint-disable-next-line no-control-regex
const OSC8 = /(\x1b\]8;[^;]*;[^\x07\x1b]*(?:\x07|\x1b\\))/;
const KEYS = {Enter: '\r', Backspace: '\x7f', Tab: '\t', Escape: '\x1b', ArrowUp: '\x1b[A', ArrowDown: '\x1b[B', ArrowRight: '\x1b[C', ArrowLeft: '\x1b[D'};

const emitter = () => {
  const listeners = new Set();
  const on = fn => { listeners.add(fn); return {dispose: () => listeners.delete(fn)}; };
  on.fire = value => { for (const fn of [...listeners]) fn(value); };
  return on;
};

export class Terminal {
  constructor(options = {}) {
    const term = this;
    this.cols = 80; this.rows = 24; this.element = null; this.text = '';
    this.onData = emitter(); this.onScroll = emitter(); this.onResize = emitter();
    this.options = new Proxy({...options}, {set(target, key, value) { target[key] = value; term._render(); return true; }});
    this.buffer = {active: {getLine: y => {
      const line = term.lines()[y];
      return line === undefined ? undefined : {translateToString: (trim, start = 0, end) => line.slice(start, end)};
    }}};
  }
  lines() { return this.text.replace(ANSI, '').replace(/\r\n/g, '\n').split('\n').map(line => line.split('\r').at(-1)); }
  loadAddon(addon) { addon.activate?.(this); }
  attachCustomKeyEventHandler(fn) { this.keyHandler = fn; }
  open(host) {
    const doc = host.ownerDocument;
    this.element = doc.createElement('div');
    this.element.className = 'xterm';
    this.textarea = doc.createElement('textarea');
    this.textarea.className = 'xterm-helper-textarea';
    this.textarea.setAttribute('aria-label', 'Terminal input');
    this.rowsEl = doc.createElement('div');
    this.rowsEl.className = 'xterm-rows';
    this.element.append(this.textarea, this.rowsEl);
    host.append(this.element);
    // xterm's element fills its host; a click anywhere on it focuses the terminal.
    host.addEventListener('mousedown', () => this.focus());
    this.textarea.addEventListener('keydown', event => {
      if (this.keyHandler && this.keyHandler(event) === false) return;
      let data = KEYS[event.key];
      if (!data && event.ctrlKey && event.key.length === 1) data = String.fromCharCode(event.key.toUpperCase().charCodeAt(0) - 64);
      else if (!data && event.key.length === 1 && !event.metaKey && !event.altKey) data = event.key;
      if (!data) return;
      event.preventDefault();
      this.onData.fire(data);
    });
    this._render();
  }
  write(data) {
    this.text += typeof data === 'string' ? data : decoder.decode(data);
    this._render();
  }
  // Each line as pieces of text, the OSC 8 link (uri) each belongs to.
  segments() {
    return this.text.replace(/\r\n/g, '\n').split('\n').map(raw => {
      const out = [];
      let uri = '';
      for (const part of raw.split('\r').at(-1).split(OSC8)) {
        if (part.startsWith('\x1b]8;')) { uri = part.replace(/^\x1b\]8;[^;]*;/, '').replace(/(\x07|\x1b\\)$/, ''); continue; }
        const text = part.replace(ANSI, '');
        if (text) out.push({text, uri});
      }
      return out;
    });
  }
  _render() {
    if (!this.rowsEl) return;
    this.rowsEl.style.fontSize = `${this.options.fontSize || 15}px`;
    const doc = this.rowsEl.ownerDocument;
    this.rowsEl.replaceChildren(...this.segments().map((pieces, y) => {
      const row = doc.createElement('div');
      let x = 1;
      for (const {text, uri} of pieces) {
        if (!uri) { row.append(text); x += text.length; continue; }
        const span = doc.createElement('span');
        span.className = 'xterm-link';
        span.textContent = text;
        const range = {start: {x, y: y + 1}, end: {x: x + text.length - 1, y: y + 1}};
        const links = () => this.options.linkHandler || {};
        span.addEventListener('mouseenter', event => links().hover?.(event, uri, range));
        span.addEventListener('mouseleave', event => links().leave?.(event, uri, range));
        span.addEventListener('click', event => links().activate?.(event, uri, range));
        row.append(span);
        x += text.length;
      }
      return row;
    }));
  }
  focus() { this.textarea?.focus(); }
  blur() { this.textarea?.blur(); }
  getSelection() { return ''; }
  clearSelection() {}
  clear() { this.text = ''; this._render(); }
  resize(cols, rows) { this.cols = cols; this.rows = rows; this.onResize.fire({cols, rows}); }
  dispose() { this.element?.remove(); this.element = null; }
}

export class FitAddon { activate() {} fit() {} proposeDimensions() { return {cols: 80, rows: 24}; } dispose() {} }
export class SearchAddon {
  activate(term) { this.term = term; }
  findNext(text) { return this.term.lines().join('\n').includes(text); }
  findPrevious(text) { return this.findNext(text); }
  clearDecorations() {}
  dispose() {}
}
export class WebLinksAddon { activate() {} dispose() {} }
