// A small Playwright-like page over happy-dom, for component tests that check behaviour
// (requests, texts, counts, states, switching) without a real browser.
// Import '../domTestEnv.mjs' before this module. There is no layout: sizes, overflow and
// scrolling stay in the *.browser.test.mjs files.
//
// Supported: page/locator .locator(css, {hasText}) .getByRole(role, {name, exact, checked})
// .getByText .getByLabel .getByTestId .filter({hasText, hasNotText, has, hasNot, visible})
// .first .last .nth .all .count .innerText .textContent .allInnerTexts .allTextContents
// .getAttribute .inputValue .isChecked .isVisible .isHidden .isDisabled .isEnabled
// .click .tap .dblclick .hover .focus .blur .fill .press .type .check .uncheck .selectOption
// .setInputFiles .dispatchEvent .evaluate .evaluateAll .waitFor({state}) .scrollIntoViewIfNeeded;
// page.keyboard.press/type, page.waitForFunction, page.waitForResponse/Request (via the fake network),
// page.evaluate. A css selector may end with :visible.
import {window} from '../domTestEnv.mjs';
import {cssValue} from './cssIndex.mjs';

const DEFAULT_TIMEOUT = 5000;
const tick = () => new Promise(resolve => setImmediate(resolve));
export async function settle(rounds = 3) { for (let i = 0; i < rounds; i++) await tick(); }
export const sleep = ms => new Promise(resolve => realSetTimeout(resolve, ms));

// Timers: long timers (polls, notice durations) run `scale` times faster, so a test does not
// wait seconds for the next poll. Order and relative timing are kept.
const realSetTimeout = globalThis.setTimeout, realSetInterval = globalThis.setInterval;
let scale = 1;
export function scaleTimers(factor) {
  scale = factor;
  const scaled = ms => (Number(ms) >= 200 && scale > 1 ? Math.max(20, Number(ms) / scale) : ms);
  globalThis.setTimeout = Object.assign((fn, ms, ...rest) => realSetTimeout(fn, scaled(ms), ...rest), {__promisify__: realSetTimeout.__promisify__});
  globalThis.setInterval = (fn, ms, ...rest) => realSetInterval(fn, scaled(ms), ...rest);
}

const BLOCK = new Set(['ADDRESS', 'ARTICLE', 'ASIDE', 'BLOCKQUOTE', 'DD', 'DETAILS', 'DIALOG', 'DIV', 'DL', 'DT', 'FIELDSET', 'FIGCAPTION', 'FIGURE', 'FOOTER', 'FORM',
  'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'HEADER', 'HR', 'LI', 'MAIN', 'NAV', 'OL', 'P', 'PRE', 'SECTION', 'SUMMARY', 'TABLE', 'TBODY', 'THEAD', 'TFOOT', 'TR', 'UL', 'LEGEND', 'CAPTION']);
const SKIP = new Set(['SCRIPT', 'STYLE', 'TEMPLATE', 'NOSCRIPT']);

// display / visibility from the inline style, then the app's CSS (testing/cssIndex.mjs), then the tag.
const INLINE_TAGS = new Set(['SPAN', 'A', 'B', 'STRONG', 'EM', 'I', 'CODE', 'SMALL', 'LABEL', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'IMG', 'SVG', 'svg', 'ABBR', 'KBD', 'MARK', 'SUB', 'SUP', 'TIME', 'S', 'U', 'BDI', 'Q']);
// Styles cannot change while one synchronous query runs: cache per element until the next microtask.
let cache = null;
function cached(el, kind, compute) {
  if (!cache) { cache = {display: new WeakMap(), visibility: new WeakMap()}; queueMicrotask(() => { cache = null; }); }
  const map = cache[kind];
  if (map.has(el)) return map.get(el);
  const value = compute(el);
  map.set(el, value);
  return value;
}
export function displayOf(el) {
  const inline = el.style?.display;
  if (inline) return inline;
  return cached(el, 'display', cssDisplay);
}
function cssDisplay(el) {
  const css = cssValue(el, 'display', window);
  if (css) return css;
  if (el.tagName === 'LI') return 'list-item';
  if (el.tagName === 'TR') return 'table-row';
  if (el.tagName === 'TD' || el.tagName === 'TH') return 'table-cell';
  if (el.tagName === 'TABLE') return 'table';
  if (BLOCK.has(el.tagName)) return 'block';
  return 'inline';
}
function visibilityOf(el) {
  for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
    const value = node.style?.visibility || cached(node, 'visibility', n => cssValue(n, 'visibility', window));
    if (value && value !== 'inherit') return value;
  }
  return 'visible';
}
export function isHidden(el) {
  if (!el.isConnected) return true;
  for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
    if (node.hidden || SKIP.has(node.tagName)) return true;
    if (node.tagName === 'INPUT' && node.type === 'hidden') return true;
    if (displayOf(node) === 'none') return true;
    // A closed <details> shows only its summary.
    const parent = node.parentElement;
    if (parent?.tagName === 'DETAILS' && !parent.open && !(node.tagName === 'SUMMARY' && parent.querySelector(':scope > summary') === node)) return true;
  }
  return visibilityOf(el) === 'hidden';
}
export const isVisible = el => !isHidden(el);

// innerText as a browser lays it out, roughly: block-level boxes (blocks, list items, and the
// items of a flex or grid container) on lines of their own, table cells separated by tabs,
// whitespace collapsed outside <pre>.
export function innerText(el) {
  // As in a browser: an element that is not rendered (display: none) gives its textContent.
  if (isHidden(el)) return el.isConnected ? el.textContent : '';
  const out = [];
  const walk = (node, pre, parentDisplay) => {
    if (node.nodeType === 3) { out.push(pre ? node.data : node.data.replace(/[ \t\n\r\f]+/g, ' ')); return; }
    if (node.nodeType !== 1 || SKIP.has(node.tagName)) return;
    if (node.hidden) return;
    const display = displayOf(node);
    if (display === 'none') return;
    if (node.tagName === 'BR') { out.push('\n'); return; }
    const blockified = /flex|grid/.test(parentDisplay) && display !== 'contents';
    const block = blockified || ['block', 'flex', 'grid', 'list-item', 'table', 'flow-root', 'table-row', 'table-caption'].includes(display);
    const cell = display === 'table-cell';
    if (block) out.push('\n');
    const isPre = pre || node.tagName === 'PRE' || node.tagName === 'TEXTAREA' || /pre/.test(node.style?.whiteSpace || '');
    if (node.tagName === 'INPUT' && ['button', 'submit', 'reset'].includes(node.type)) out.push(node.value);
    const childDisplay = display === 'contents' ? parentDisplay : display;
    for (const child of node.childNodes) walk(child, isPre, childDisplay);
    if (cell) out.push('\t');
    if (block) out.push('\n');
  };
  walk(el, false, '');
  return out.join('').split('\n').map(line => line.replace(/^[ \t]+|[ \t]+$/g, '').replace(/ {2,}/g, ' ')).filter(Boolean).join('\n').trim();
}
// el.innerText in the page (tests that read it inside evaluate / evaluateAll) as above, not
// happy-dom's, which ignores CSS display (flex items run together).
{
  const native = Object.getOwnPropertyDescriptor(window.HTMLElement.prototype, 'innerText');
  Object.defineProperty(window.HTMLElement.prototype, 'innerText', {configurable: true, get() { return innerText(this); }, set(value) { native.set.call(this, value); }});
}
const norm = text => String(text ?? '').replace(/\s+/g, ' ').trim();
const textOf = el => norm(el.tagName === 'INPUT' && ['button', 'submit', 'reset'].includes(el.type) ? el.value : el.textContent);

function matches(value, expected, exact) {
  if (expected instanceof RegExp) return expected.test(value);
  const a = norm(value), b = norm(expected);
  return exact ? a === b : a.toLowerCase().includes(b.toLowerCase());
}

// Roles: an explicit role attribute, or the element's implicit role.
const IMPLICIT = [
  ['button', 'button, input[type=button], input[type=submit], input[type=reset], input[type=image], summary'],
  ['link', 'a[href], area[href]'],
  ['heading', 'h1, h2, h3, h4, h5, h6'],
  ['checkbox', 'input[type=checkbox]'], ['radio', 'input[type=radio]'],
  ['textbox', 'textarea, input:not([type]), input[type=text], input[type=email], input[type=tel], input[type=url], input[type=password]'],
  ['searchbox', 'input[type=search]'], ['spinbutton', 'input[type=number]'], ['slider', 'input[type=range]'],
  ['combobox', 'select:not([multiple]):not([size])'], ['listbox', 'select[multiple], select[size], datalist'], ['option', 'option'],
  ['list', 'ul, ol, menu'], ['listitem', 'li'], ['table', 'table'], ['row', 'tr'], ['cell', 'td'], ['columnheader', 'th'],
  ['img', 'img[alt]:not([alt=""])'], ['group', 'fieldset, details, optgroup'], ['navigation', 'nav'], ['dialog', 'dialog'],
  ['progressbar', 'progress'], ['separator', 'hr'], ['form', 'form'], ['article', 'article'], ['region', 'section[aria-label], section[aria-labelledby]'],
  ['banner', 'header'], ['contentinfo', 'footer'], ['main', 'main'], ['complementary', 'aside'], ['figure', 'figure'], ['term', 'dt'], ['definition', 'dd'],
];
export function roleOf(el) {
  const explicit = el.getAttribute('role');
  if (explicit) return explicit.split(/\s+/)[0];
  for (const [role, selector] of IMPLICIT) if (el.matches(selector)) return role;
  return '';
}
const NAME_FROM_CONTENT = new Set(['button', 'link', 'tab', 'menuitem', 'menuitemradio', 'menuitemcheckbox', 'heading', 'option', 'cell', 'columnheader', 'row', 'radio', 'checkbox', 'switch', 'treeitem', 'tooltip', 'listitem', 'gridcell', 'rowheader', 'term']);
function labelText(el) {
  const doc = el.ownerDocument, parts = [];
  if (el.id) for (const label of doc.querySelectorAll(`label[for="${CSS.escape(el.id)}"]`)) parts.push(contentName(label, el));
  const wrapping = el.closest('label');
  if (wrapping) parts.push(contentName(wrapping, el));
  return norm(parts.join(' '));
}
function contentName(el, skip) {
  const parts = [];
  const walk = node => {
    if (node === skip) return;
    if (node.nodeType === 3) { parts.push(node.data); return; }
    if (node.nodeType !== 1 || SKIP.has(node.tagName) || node.hidden || displayOf(node) === 'none' || node.getAttribute('aria-hidden') === 'true') return;
    const label = node.getAttribute('aria-label');
    if (label && node !== el) { parts.push(' ' + label + ' '); return; }
    if (node.tagName === 'IMG') { parts.push(node.getAttribute('alt') || ''); return; }
    if (node.tagName === 'INPUT' && ['button', 'submit', 'reset'].includes(node.type)) { parts.push(node.value); return; }
    if (BLOCK.has(node.tagName)) parts.push(' ');
    for (const child of node.childNodes) walk(child);
    if (BLOCK.has(node.tagName)) parts.push(' ');
  };
  for (const child of el.childNodes) walk(child);
  return norm(parts.join(''));
}
export function accessibleName(el) {
  const doc = el.ownerDocument;
  const labelledby = el.getAttribute('aria-labelledby');
  if (labelledby) {
    const text = labelledby.split(/\s+/).map(id => doc.getElementById(id)).filter(Boolean).map(node => contentName(node) || textOf(node)).join(' ');
    if (norm(text)) return norm(text);
  }
  const label = el.getAttribute('aria-label');
  if (label && norm(label)) return norm(label);
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)) {
    if (el.tagName === 'INPUT' && ['button', 'submit', 'reset'].includes(el.type)) return norm(el.value);
    const text = labelText(el);
    if (text) return text;
    return norm(el.getAttribute('title') || el.getAttribute('placeholder') || '');
  }
  const role = roleOf(el);
  if (NAME_FROM_CONTENT.has(role)) { const text = contentName(el); if (text) return text; }
  if (el.tagName === 'IMG') return norm(el.getAttribute('alt'));
  if (el.tagName === 'FIELDSET') { const legend = el.querySelector(':scope > legend'); if (legend) return textOf(legend); }
  return norm(el.getAttribute('title') || '');
}
const hiddenForRoles = el => {
  for (let node = el; node && node.nodeType === 1; node = node.parentElement) if (node.getAttribute('aria-hidden') === 'true') return true;
  return isHidden(el);
};
const checkedOf = el => {
  if (el.tagName === 'INPUT' && ['checkbox', 'radio'].includes(el.type)) return el.checked;
  const aria = el.getAttribute('aria-checked') ?? el.getAttribute('aria-pressed') ?? el.getAttribute('aria-selected');
  if (aria !== null) return aria === 'true';
  // Element Plus marks its wrapper.
  const wrap = el.closest('.el-checkbox, .el-radio, .el-radio-button, .el-switch, .el-checkbox-button');
  if (wrap) return wrap.classList.contains('is-checked');
  return false;
};
const disabledOf = el => {
  for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
    if (node.disabled === true || node.getAttribute('aria-disabled') === 'true') return true;
    if (node.tagName === 'FIELDSET' && node.disabled) return true;
  }
  return false;
};

// Visible text matching for getByText: the innermost elements whose text matches.
function textMatches(root, expected, exact) {
  const out = [];
  const visit = el => {
    if (SKIP.has(el.tagName)) return false;
    let childMatched = false;
    for (const child of el.children) if (visit(child)) childMatched = true;
    if (childMatched) return true;
    // Like Playwright's text engine, the scope element itself matches when no descendant does.
    if (el === root && (el.nodeType !== 1 || el.tagName === 'BODY' || el.tagName === 'HTML')) return false;
    if (matches(textOf(el), expected, exact)) { out.push(el); return true; }
    return false;
  };
  visit(root);
  return out;
}

function queryCss(root, selector) {
  if (selector.trim() === '..') return root.parentElement ? [root.parentElement] : [];
  let visibleOnly = false;
  const parts = selector.trim();
  let css = parts;
  if (/:visible$/.test(css)) { visibleOnly = true; css = css.replace(/:visible$/, ''); }
  if (/:visible\b/.test(css)) {
    // a:visible b  → split at the first :visible
    const [head, ...rest] = css.split(/:visible/);
    const heads = queryCss(root, head + ':visible');
    return unique(heads.flatMap(el => queryCss(el, rest.join(':visible').trim() || '*')));
  }
  let list;
  if (/^\s*>/.test(css)) list = [...root.querySelectorAll(`:scope ${css}`)];
  else list = [...root.querySelectorAll(css)];
  return visibleOnly ? list.filter(isVisible) : list;
}
const unique = list => [...new Set(list)];

export class Locator {
  constructor(page, resolve, description) { this._page = page; this._resolve = resolve; this._description = description; }
  _all() { return this._resolve(); }
  page() { return this._page; }
  toString() { return this._description; }
  _chain(fn, description) { return new Locator(this._page, () => unique(this._all().flatMap(fn)), `${this._description} >> ${description}`); }

  locator(selector, options = {}) {
    if (selector instanceof Locator) return this._chain(el => selector._all().filter(x => el !== x && el.contains(x)), selector._description);
    const found = this._chain(el => queryCss(el, selector), selector);
    return Object.keys(options).length ? found.filter(options) : found;
  }
  getByRole(role, {name, exact = false, checked, selected, level, disabled, pressed, expanded, includeHidden = false} = {}) {
    return this._chain(root => [...root.querySelectorAll('*')].filter(el => {
      if (roleOf(el) !== role) return false;
      if (!includeHidden && hiddenForRoles(el)) return false;
      if (name !== undefined && !matches(accessibleName(el), name, exact)) return false;
      if (checked !== undefined && checkedOf(el) !== checked) return false;
      if (selected !== undefined && (el.getAttribute('aria-selected') === 'true' || el.selected === true) !== selected) return false;
      if (pressed !== undefined && (el.getAttribute('aria-pressed') === 'true') !== pressed) return false;
      if (expanded !== undefined && (el.getAttribute('aria-expanded') === 'true') !== expanded) return false;
      if (disabled !== undefined && disabledOf(el) !== disabled) return false;
      if (level !== undefined && Number(el.tagName.slice(1) || el.getAttribute('aria-level')) !== level) return false;
      return true;
    }), `getByRole(${role}, ${name})`);
  }
  getByText(text, {exact = false} = {}) { return this._chain(root => textMatches(root, text, exact), `getByText(${text})`); }
  getByLabel(text, {exact = false} = {}) {
    return this._chain(root => [...root.querySelectorAll('input, textarea, select, [role=textbox], [role=combobox], [role=switch], [role=checkbox], [role=radio], [role=slider], [role=spinbutton], [contenteditable], [aria-label], [aria-labelledby]')]
      .filter(el => {
        const label = el.getAttribute('aria-labelledby') ? accessibleName(el) : el.getAttribute('aria-label') || labelText(el);
        return label && matches(label, text, exact);
      }), `getByLabel(${text})`);
  }
  getByPlaceholder(text, {exact = false} = {}) { return this._chain(root => [...root.querySelectorAll('[placeholder]')].filter(el => matches(el.getAttribute('placeholder'), text, exact)), `getByPlaceholder(${text})`); }
  getByTestId(id) { return this._chain(root => [...root.querySelectorAll(`[data-testid="${id}"]`)], `getByTestId(${id})`); }
  getByTitle(text, {exact = false} = {}) { return this._chain(root => [...root.querySelectorAll('[title]')].filter(el => matches(el.getAttribute('title'), text, exact)), `getByTitle(${text})`); }
  filter({hasText, hasNotText, has, hasNot, visible} = {}) {
    return new Locator(this._page, () => this._all().filter(el => {
      if (hasText !== undefined && !matches(textOf(el), hasText, false)) return false;
      if (hasNotText !== undefined && matches(textOf(el), hasNotText, false)) return false;
      if (has && !has._relativeTo(el).length) return false;
      if (hasNot && hasNot._relativeTo(el).length) return false;
      if (visible !== undefined && isVisible(el) !== visible) return false;
      return true;
    }), `${this._description} >> filter`);
  }
  // `has` locators are written from the page; re-run them inside one element.
  _relativeTo(el) { return this._all().filter(x => x !== el && el.contains(x)); }
  first() { return new Locator(this._page, () => this._all().slice(0, 1), `${this._description} >> first`); }
  last() { return new Locator(this._page, () => this._all().slice(-1), `${this._description} >> last`); }
  nth(i) { return new Locator(this._page, () => { const all = this._all(); return all.slice(i < 0 ? all.length + i : i).slice(0, 1); }, `${this._description} >> nth=${i}`); }
  and(other) { return new Locator(this._page, () => { const theirs = new Set(other._all()); return this._all().filter(el => theirs.has(el)); }, `${this._description} >> and`); }
  or(other) { return new Locator(this._page, () => unique([...this._all(), ...other._all()]), `${this._description} >> or`); }
  async all() { await settle(1); return this._all().map((_, i) => this.nth(i)); }
  async count() { await settle(1); return this._all().length; }

  // One element, waiting for it like Playwright does (and strict: two matches are an error).
  async _one({visible = true, timeout = DEFAULT_TIMEOUT, enabled = false} = {}) {
    const deadline = Date.now() + timeout;
    let last = '';
    for (;;) {
      await settle(1);
      const all = this._all();
      if (all.length > 1) throw new Error(`strict mode violation: ${this._description} resolved to ${all.length} elements`);
      const el = all[0];
      if (el && (!visible || isVisible(el)) && (!enabled || !disabledOf(el))) return el;
      last = el ? (isVisible(el) ? 'disabled' : 'hidden') : 'not found';
      if (Date.now() > deadline) throw new Error(`Timeout ${timeout}ms waiting for ${this._description} (${last})`);
      await sleep(5);
    }
  }
  async waitFor({state = 'visible', timeout = DEFAULT_TIMEOUT} = {}) {
    const deadline = Date.now() + timeout;
    for (;;) {
      await settle(1);
      const all = this._all();
      const ok = state === 'attached' ? all.length > 0 : state === 'detached' ? all.length === 0
        : state === 'visible' ? all.some(isVisible) : all.every(isHidden);
      if (ok) return;
      if (Date.now() > deadline) throw new Error(`Timeout ${timeout}ms waiting for ${this._description} to be ${state} (${all.length} found)`);
      await sleep(5);
    }
  }
  async innerText() { return innerText(await this._one({visible: false})); }
  async textContent() { return (await this._one({visible: false})).textContent; }
  async allInnerTexts() { await settle(1); return this._all().map(innerText); }
  async allTextContents() { await settle(1); return this._all().map(el => el.textContent); }
  async getAttribute(name) { return (await this._one({visible: false})).getAttribute(name); }
  async inputValue() { return (await this._one({visible: false})).value; }
  async isChecked() { return checkedOf(await this._one({visible: false})); }
  async isVisible() { await settle(1); const el = this._all()[0]; return Boolean(el && isVisible(el)); }
  async isHidden() { return !(await this.isVisible()); }
  async isDisabled() { return disabledOf(await this._one({visible: false})); }
  async isEnabled() { return !disabledOf(await this._one({visible: false})); }
  async evaluate(fn, arg) { return fn(await this._one({visible: false}), arg); }
  async evaluateAll(fn, arg) { await settle(1); return fn(this._all(), arg); }
  async scrollIntoViewIfNeeded() { await this._one({visible: false}); }
  async boundingBox() { throw new Error('boundingBox: no layout in happy-dom; keep size checks in the browser test'); }
  async screenshot() {}

  async hover() { const el = await this._one(); hover(el); await settle(); }
  async focus() { const el = await this._one(); focus(this._page, el); await settle(); }
  async blur() { const el = await this._one({visible: false}); blur(this._page, el); await settle(); }
  async click(options = {}) { const el = await this._one({enabled: !options.force}); await click(this._page, el, options); }
  async tap(options = {}) { const el = await this._one({enabled: !options.force}); await click(this._page, el, {...options, touch: true}); }
  async dblclick(options = {}) { const el = await this._one({enabled: true}); await click(this._page, el, options); await click(this._page, el, {...options, detail: 2}); el.dispatchEvent(new window.MouseEvent('dblclick', {bubbles: true, cancelable: true})); await settle(); }
  async fill(value) {
    const el = await this._one({enabled: true});
    const target = editable(el);
    focus(this._page, target);
    if (target.isContentEditable || target.getAttribute('contenteditable') !== null) target.textContent = value;
    else setValue(target, value);
    target.dispatchEvent(new window.InputEvent('input', {bubbles: true, data: value, inputType: 'insertText'}));
    // Like Playwright: date-like inputs get their change event at once, text inputs on blur.
    if (target.tagName === 'INPUT' && ['date', 'time', 'datetime-local', 'month', 'week', 'color', 'range'].includes(target.type)) target.dispatchEvent(new window.Event('change', {bubbles: true}));
    else target.__filled = true;
    await settle();
  }
  async clear() { await this.fill(''); }
  async type(text) { const el = await this._one({enabled: true}); focus(this._page, editable(el)); await this._page.keyboard.type(text); }
  pressSequentially(text) { return this.type(text); }
  async press(key) { const el = await this._one({visible: false}); const target = editable(el); if (this._page.activeElement() !== target) focus(this._page, target); await this._page.keyboard.press(key); }
  async check() { const el = await this._one({visible: false}); if (!checkedOf(el)) await click(this._page, clickTarget(el), {}); await settle(); if (!checkedOf(el)) throw new Error(`check: ${this._description} did not become checked`); }
  async uncheck() { const el = await this._one({visible: false}); if (checkedOf(el)) await click(this._page, clickTarget(el), {}); await settle(); if (checkedOf(el)) throw new Error(`uncheck: ${this._description} stayed checked`); }
  async setChecked(value) { return value ? this.check() : this.uncheck(); }
  async selectOption(value) {
    const el = await this._one({enabled: true});
    const wanted = [].concat(value).map(v => (typeof v === 'object' ? v : {value: v, label: v}));
    const options = [...el.options];
    const chosen = options.filter(o => wanted.some(w => (w.value !== undefined && o.value === w.value) || (w.label !== undefined && norm(o.textContent) === norm(w.label)) || (w.index !== undefined && o.index === w.index)));
    if (!chosen.length) throw new Error(`selectOption: no option ${JSON.stringify(value)} in ${this._description}`);
    for (const o of options) o.selected = chosen.includes(o);
    el.dispatchEvent(new window.Event('input', {bubbles: true}));
    el.dispatchEvent(new window.Event('change', {bubbles: true}));
    await settle();
    return chosen.map(o => o.value);
  }
  async setInputFiles(files) {
    const el = await this._one({visible: false});
    const list = [].concat(files).map(f => new window.File([new Uint8Array(f.buffer)], f.name, {type: f.mimeType || ''}));
    Object.defineProperty(el, 'files', {value: Object.assign(list, {item: i => list[i]}), configurable: true});
    el.dispatchEvent(new window.Event('input', {bubbles: true}));
    el.dispatchEvent(new window.Event('change', {bubbles: true}));
    await settle();
  }
  // HTML drag and drop, as Playwright's dragTo does for a draggable element: dragstart on this
  // element, dragenter / dragover / drop on the target at targetPosition (client coordinates;
  // there is no layout, the target sits at 0, 0), then dragend. One DataTransfer throughout.
  async dragTo(target, {targetPosition = {x: 0, y: 0}} = {}) {
    const el = await this._one(), to = await target._one({visible: false});
    const store = new Map();
    const dataTransfer = {dropEffect: 'none', effectAllowed: 'all', files: [], items: [],
      get types() { return [...store.keys()]; }, setData(type, value) { store.set(type, String(value)); }, getData(type) { return store.get(type) ?? ''; },
      clearData(type) { if (type) store.delete(type); else store.clear(); }, setDragImage() {}};
    const fire = (node, type, at = {}) => { const event = new window.MouseEvent(type, {bubbles: true, cancelable: true, composed: true, view: window, ...at}); Object.defineProperty(event, 'dataTransfer', {value: dataTransfer}); node.dispatchEvent(event); return event; };
    const at = {clientX: targetPosition.x, clientY: targetPosition.y};
    fire(el, 'dragstart');
    await settle();
    fire(to, 'dragenter', at);
    fire(to, 'dragover', at);
    await settle();
    fire(to, 'drop', at);
    fire(el, 'dragend');
    await settle();
  }
  async dispatchEvent(type, init = {}) {
    const el = await this._one({visible: false});
    const Ctor = /^(click|mouse|dblclick|contextmenu)/.test(type) ? window.MouseEvent : /^pointer/.test(type) ? window.PointerEvent : /^key/.test(type) ? window.KeyboardEvent : /^drag|^drop/.test(type) ? window.DragEvent : window.Event;
    el.dispatchEvent(new Ctor(type, {bubbles: true, cancelable: true, composed: true, ...init}));
    await settle();
  }
}

const editable = el => (['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName) || el.getAttribute('contenteditable') !== null ? el : el.querySelector('input, textarea, [contenteditable]') || el);
const clickTarget = el => (el.tagName === 'INPUT' && el.closest('label') ? el.closest('label') : el);
function setValue(el, value) {
  const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value')?.set?.call(el, value) ?? (el.value = value);
}
function focusable(el) {
  return el.matches('input, textarea, select, button, a[href], [tabindex], [contenteditable]') ? el : null;
}
function focus(page, el) {
  const target = focusable(el) || el.closest('input, textarea, select, button, a[href], [tabindex], [contenteditable]');
  const previous = page.activeElement();
  // A click on a plain element inside the focused widget (xterm's screen over its textarea) keeps focus.
  if (!target && previous && el.contains(previous)) return;
  if (previous && previous !== target) blur(page, previous);
  target?.focus?.();
}
function blur(page, el) {
  if (el.__filled) { el.__filled = false; el.dispatchEvent(new window.Event('change', {bubbles: true})); }
  el.blur?.();
}
// The element the pointer is over: moving onto another one leaves it first, as in a browser.
let hovered = null;
// The pointer moved off to an empty spot (page.mouse.move(5, 5) in a browser test): the element it was over is left.
export function leaveHover() {
  const el = hovered;
  hovered = null;
  if (!el?.isConnected) return;
  const init = {bubbles: true, cancelable: true, composed: true, view: window};
  el.dispatchEvent(new window.PointerEvent('pointerout', init));
  el.dispatchEvent(new window.PointerEvent('pointerleave', {...init, bubbles: false}));
  el.dispatchEvent(new window.MouseEvent('mouseout', init));
  el.dispatchEvent(new window.MouseEvent('mouseleave', {...init, bubbles: false}));
}
function hover(el) {
  const init = {bubbles: true, cancelable: true, composed: true, view: window};
  if (hovered && hovered !== el && hovered.isConnected && !hovered.contains(el)) {
    hovered.dispatchEvent(new window.PointerEvent('pointerout', init));
    hovered.dispatchEvent(new window.PointerEvent('pointerleave', {...init, bubbles: false}));
    hovered.dispatchEvent(new window.MouseEvent('mouseout', init));
    hovered.dispatchEvent(new window.MouseEvent('mouseleave', {...init, bubbles: false}));
  }
  hovered = el;
  el.dispatchEvent(new window.PointerEvent('pointerover', init));
  el.dispatchEvent(new window.PointerEvent('pointerenter', {...init, bubbles: false}));
  el.dispatchEvent(new window.MouseEvent('mouseover', init));
  el.dispatchEvent(new window.MouseEvent('mouseenter', {...init, bubbles: false}));
  el.dispatchEvent(new window.PointerEvent('pointermove', init));
  el.dispatchEvent(new window.MouseEvent('mousemove', init));
}
async function click(page, el, {touch = false, button = 'left', modifiers = [], detail = 1} = {}) {
  const init = {bubbles: true, cancelable: true, composed: true, view: window, button: button === 'right' ? 2 : 0, detail,
    ctrlKey: modifiers.includes('Control'), shiftKey: modifiers.includes('Shift'), altKey: modifiers.includes('Alt'), metaKey: modifiers.includes('Meta')};
  if (!touch) hover(el);
  el.dispatchEvent(new window.PointerEvent('pointerdown', {...init, pointerType: touch ? 'touch' : 'mouse', isPrimary: true}));
  if (touch) el.dispatchEvent(new window.TouchEvent('touchstart', {bubbles: true, cancelable: true}));
  const before = window.document.activeElement;
  const down = el.dispatchEvent(new window.MouseEvent('mousedown', init));
  // The default action moves focus to what was clicked, unless a handler already moved it.
  if (down && window.document.activeElement === before) focus(page, el);
  el.dispatchEvent(new window.PointerEvent('pointerup', {...init, pointerType: touch ? 'touch' : 'mouse', isPrimary: true}));
  if (touch) el.dispatchEvent(new window.TouchEvent('touchend', {bubbles: true, cancelable: true}));
  el.dispatchEvent(new window.MouseEvent('mouseup', init));
  if (button === 'right') el.dispatchEvent(new window.MouseEvent('contextmenu', init));
  else {
    // A disabled form control gets no click; a click on a <label> activates its control (happy-dom does this).
    el.dispatchEvent(new window.MouseEvent('click', init));
  }
  await settle();
}

const KEY_CODES = {Enter: 13, Escape: 27, Tab: 9, Backspace: 8, Delete: 46, ArrowUp: 38, ArrowDown: 40, ArrowLeft: 37, ArrowRight: 39, Home: 36, End: 35, PageUp: 33, PageDown: 34, ' ': 32, Space: 32};
function keyInit(combo) {
  const parts = combo.split('+'), key = parts.pop() === '' ? '+' : combo.split('+').pop();
  const mods = parts;
  const k = key === 'Space' ? ' ' : key;
  return {key: k, code: k.length === 1 ? (/[a-z]/i.test(k) ? `Key${k.toUpperCase()}` : k === ' ' ? 'Space' : k) : k, keyCode: KEY_CODES[key] || k.toUpperCase().charCodeAt(0),
    ctrlKey: mods.includes('Control') || (mods.includes('ControlOrMeta')), shiftKey: mods.includes('Shift'), altKey: mods.includes('Alt'), metaKey: mods.includes('Meta')};
}

const downloads = new Set();
const anchorClick = window.HTMLAnchorElement.prototype.click;
window.HTMLAnchorElement.prototype.click = function click() {
  if (this.hasAttribute('download') && downloads.size) { for (const fn of [...downloads]) fn(this); return; }
  if (this.hasAttribute('download')) return; // nowhere to save a file in the tests
  return anchorClick.call(this);
};

export class DomPage {
  constructor(network) {
    this.network = network;
    const root = () => [window.document.body];
    this._root = new Locator(this, root, 'page');
    const page = this;
    this.keyboard = {
      async press(combo) {
        const target = page.activeElement() || window.document.body, init = {bubbles: true, cancelable: true, composed: true, ...keyInit(combo)};
        const down = target.dispatchEvent(new window.KeyboardEvent('keydown', init));
        const printable = init.key.length === 1 && !init.ctrlKey && !init.metaKey && !init.altKey;
        if (down && printable) target.dispatchEvent(new window.KeyboardEvent('keypress', init));
        if (down && printable && ['INPUT', 'TEXTAREA'].includes(target.tagName)) {
          setValue(target, target.value + init.key); target.__filled = true;
          target.dispatchEvent(new window.InputEvent('input', {bubbles: true, data: init.key, inputType: 'insertText'}));
        } else if (down && init.key === 'Backspace' && ['INPUT', 'TEXTAREA'].includes(target.tagName)) {
          setValue(target, target.value.slice(0, -1)); target.__filled = true;
          target.dispatchEvent(new window.InputEvent('input', {bubbles: true, inputType: 'deleteContentBackward'}));
        } else if (down && init.key === 'Enter' && target.tagName === 'INPUT' && target.form) {
          target.form.dispatchEvent(new window.Event('submit', {bubbles: true, cancelable: true}));
        } else if (down && init.key === 'Tab' && target !== window.document.body) {
          // Focus moves on, as in a browser: the field left gets its change and blur.
          const order = [...window.document.querySelectorAll('input, textarea, select, button, a[href], [tabindex]:not([tabindex="-1"])')].filter(el => !el.disabled && isVisible(el));
          const next = order[order.indexOf(target) + (init.shiftKey ? -1 : 1)];
          blur(page, target);
          next?.focus?.();
        }
        target.dispatchEvent(new window.KeyboardEvent('keyup', init));
        await settle();
      },
      async type(text) { for (const ch of text) await this.press(ch === ' ' ? ' ' : ch); },
      async down() {}, async up() {},
    };
  }
  activeElement() { const el = window.document.activeElement; return el && el !== window.document.body ? el : null; }
  locator(selector, options) { return this._root.locator(selector, options); }
  getByRole(...args) { return this._root.getByRole(...args); }
  getByText(...args) { return this._root.getByText(...args); }
  getByLabel(...args) { return this._root.getByLabel(...args); }
  getByPlaceholder(...args) { return this._root.getByPlaceholder(...args); }
  getByTestId(...args) { return this._root.getByTestId(...args); }
  getByTitle(...args) { return this._root.getByTitle(...args); }
  async evaluate(fn, arg) { await settle(1); return fn(arg); }
  async waitForTimeout(ms) { await sleep(ms); await settle(); }
  async waitForFunction(fn, arg, {timeout = DEFAULT_TIMEOUT} = {}) {
    const deadline = Date.now() + timeout;
    for (;;) {
      await settle(1);
      const value = await fn(arg);
      if (value) return value;
      if (Date.now() > deadline) throw new Error(`waitForFunction timed out: ${fn}`);
      await sleep(5);
    }
  }
  // 'download': a click on an <a download> (in the page or not), like the browser's download event.
  waitForEvent(name, {timeout = DEFAULT_TIMEOUT} = {}) {
    if (name !== 'download') throw new Error(`waitForEvent(${name}) is not supported`);
    return new Promise((resolve, reject) => {
      const timer = realSetTimeout(() => { downloads.delete(entry); reject(new Error('waitForEvent(download) timed out')); }, timeout);
      const entry = a => { clearTimeout(timer); downloads.delete(entry); resolve({url: () => a.href, suggestedFilename: () => a.download || decodeURIComponent(new URL(a.href, window.location.href).pathname.split('/').pop())}); };
      downloads.add(entry);
    });
  }
  waitForResponse(predicate, options) { return this.network.waitFor('response', predicate, options); }
  waitForRequest(predicate, options) { return this.network.waitFor('request', predicate, options); }
  url() { return window.location.href; }
}

// Wait until fn() returns a truthy value (or stops throwing).
export async function waitUntil(fn, {timeout = DEFAULT_TIMEOUT, message = String(fn)} = {}) {
  const deadline = Date.now() + timeout;
  let error;
  for (;;) {
    await settle(1);
    try { const value = await fn(); if (value) return value; error = null; } catch (e) { error = e; }
    if (Date.now() > deadline) throw error || new Error(`waitUntil timed out: ${message}`);
    await sleep(5);
  }
}
