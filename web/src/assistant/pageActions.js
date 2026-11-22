// Page tool requests, run in the user's tab. They read, highlight and fill; they never
// press a button, so saving, submitting, deleting and confirming stay with the user.
import { ASSISTANT_ROOT, capturePageContext, clean, currentScope, regionElement } from './pageContext.js';
import { liveActions, rendered } from './pageRegistry.js';

const FIELD_SELECTOR = 'input:not([type=hidden]):not([type=file]):not([type=button]):not([type=submit]):not([type=reset]), textarea, select, [contenteditable="true"]';
const CLICKABLE_SELECTOR = 'button, [role=button], a[href], [role=tab], [role=menuitem]';
const FLASH_CLASS = 'ob-assistant-flash';

export function flash(el, ms = 2600) {
  if (!el) return;
  el.classList.remove(FLASH_CLASS);
  void el.offsetWidth;
  el.classList.add(FLASH_CLASS);
  window.setTimeout(() => el.classList.remove(FLASH_CLASS), ms);
}

function normalized(text) {
  return clean(text).replace(/[：:*＊]\s*$/, '').toLowerCase();
}

function visibleIn(root, selector) {
  return [...root.querySelectorAll(selector)].filter(el => !el.closest(ASSISTANT_ROOT) && rendered(el));
}

function fieldLabel(el) {
  const aria = el.getAttribute('aria-label');
  if (aria) return clean(aria);
  if (el.id) {
    const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (label) return clean(label.textContent);
  }
  const wrapping = el.closest('label');
  if (wrapping) {
    const named = wrapping.querySelector(':scope > span, :scope > em, :scope > strong');
    if (named && !named.contains(el) && clean(named.textContent)) return clean(named.textContent);
    const copy = wrapping.cloneNode(true);
    copy.querySelectorAll('input, textarea, select, small').forEach(node => node.remove());
    const text = clean(copy.textContent);
    if (text) return text;
  }
  const item = el.closest('.el-form-item, .ob-field, .field, .form-row, .setting-row, [data-field]');
  const named = item?.querySelector('.el-form-item__label, label, .ob-field-label, .field-label, dt, strong, span');
  if (named && !named.contains(el)) return clean(named.textContent);
  return precedingLabel(el) || clean(el.getAttribute('placeholder') || el.getAttribute('name') || '');
}

const LABEL_TAG = /^(LABEL|SPAN|STRONG|DT|H3|H4|H5|H6|P)$/;

// Most forms here are `<div><label>名称</label><el-input/></div>`: the label precedes the
// component wrapper, not the input. Climb while the container holds only this field.
function precedingLabel(el) {
  let node = el;
  for (let depth = 0; node && depth < 6; depth += 1) {
    const prev = node.previousElementSibling;
    const cls = String(prev?.getAttribute('class') || '');
    if (prev && (LABEL_TAG.test(prev.tagName) || /label|title/i.test(cls)) && !/prefix|suffix/i.test(cls) && !prev.querySelector(FIELD_SELECTOR)) {
      const text = clean(prev.textContent);
      if (text && text.length <= 60) return text;
    }
    const parent = node.parentElement;
    if (!parent || parent.matches('form, body, [role=dialog], .el-dialog__body, .el-drawer__body') || parent.querySelectorAll(FIELD_SELECTOR).length > 1) break;
    node = parent;
  }
  return '';
}

function fieldKind(el) {
  if (el.isContentEditable) return 'rich-text';
  if (el.tagName === 'SELECT') return 'select';
  if (el.tagName === 'TEXTAREA') return 'textarea';
  if (el.closest('.el-select, .el-cascader, .el-date-editor')) return 'picker';
  return (el.getAttribute('type') || 'text').toLowerCase();
}

function fieldValue(el, kind) {
  if (kind === 'password') return el.value ? '••••' : '';
  if (kind === 'checkbox' || kind === 'radio') return el.checked;
  if (kind === 'rich-text') return clean(el.textContent).slice(0, 300);
  return String(el.value ?? '').slice(0, 500);
}

function describeFields(root) {
  return visibleIn(root, FIELD_SELECTOR).slice(0, 80).map(el => {
    const kind = fieldKind(el);
    const item = {label: fieldLabel(el), type: kind, value: fieldValue(el, kind)};
    if (kind === 'select') item.options = [...el.options].slice(0, 40).map(option => clean(option.textContent));
    if (el.disabled || el.readOnly) item.readonly = true;
    return item;
  }).filter(item => item.label);
}

function describeButtons(root) {
  const seen = new Set();
  return visibleIn(root, CLICKABLE_SELECTOR).map(el => clean(el.getAttribute('aria-label') || el.textContent || el.title))
    .filter(text => text && text.length <= 40 && !seen.has(text) && seen.add(text)).slice(0, 60);
}

function bestMatch(candidates, wanted, textOf) {
  const target = normalized(wanted);
  let best = null;
  let bestScore = 0;
  for (const el of candidates) {
    const text = normalized(textOf(el));
    if (!text) continue;
    const score = text === target ? 3 : text.startsWith(target) ? 2 : text.includes(target) || target.includes(text) && text.length > 1 ? 1 : 0;
    if (score > bestScore) { best = el; bestScore = score; }
    if (score === 3) break;
  }
  return best;
}

function regionsFor(region) {
  const first = regionElement(region);
  if (region || first.of === 'page') return [first.element];
  return [first.element, regionElement('page').element];
}

function read(params, {visibleChars}) {
  const context = capturePageContext({visibleChars: Math.min(20000, visibleChars * 3), region: params.region || ''});
  const {element} = regionElement(params.region || '');
  return {ok: true, context, fields: describeFields(element), buttons: describeButtons(element)};
}

function locate(params) {
  for (const root of regionsFor('')) {
    const candidates = visibleIn(root, `${CLICKABLE_SELECTOR}, ${FIELD_SELECTOR}, label, h1, h2, h3, h4, th, td, li, [data-ob-key], [aria-label]`);
    const el = bestMatch(candidates, params.target, node => node.matches(FIELD_SELECTOR) ? fieldLabel(node) : (node.getAttribute('aria-label') || node.textContent || node.title));
    if (el) {
      el.scrollIntoView({block: 'center', behavior: 'smooth'});
      flash(el);
      return {ok: true, located: clean(el.getAttribute('aria-label') || el.textContent || fieldLabel(el)).slice(0, 120), tag: el.tagName.toLowerCase()};
    }
  }
  const {element} = regionElement('');
  return {ok: false, error: 'not_found', message: `页面上没有找到「${params.target}」`, buttons: describeButtons(element).slice(0, 30)};
}

function setNative(el, value) {
  const proto = el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : el.tagName === 'SELECT' ? window.HTMLSelectElement.prototype : window.HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value')?.set?.call(el, value);
}

function fill(params) {
  for (const root of regionsFor('')) {
    const el = bestMatch(visibleIn(root, FIELD_SELECTOR), params.label, fieldLabel);
    if (!el) continue;
    const kind = fieldKind(el);
    if (el.disabled || el.readOnly) return {ok: false, error: 'field_readonly', message: `「${fieldLabel(el)}」当前不可编辑`};
    // A switch or radio often saves the moment it changes, so it stays with the user like a button.
    if (kind === 'picker' || kind === 'rich-text' || kind === 'checkbox' || kind === 'radio') {
      el.scrollIntoView({block: 'center', behavior: 'smooth'});
      flash(el.closest('.el-select, .el-cascader, .el-date-editor, .el-switch, .el-checkbox, .el-radio') || el);
      return {ok: false, error: 'field_unsupported', message: `「${fieldLabel(el)}」是选择组件、开关或富文本，不能代为填写，已高亮，请让用户自己选择或输入`};
    }
    const value = String(params.value ?? '');
    if (kind === 'select') {
      const option = [...el.options].find(item => item.value === value || clean(item.textContent) === clean(value));
      if (!option) return {ok: false, error: 'option_not_found', options: [...el.options].map(item => clean(item.textContent))};
      setNative(el, option.value);
    } else setNative(el, value);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.scrollIntoView({block: 'center', behavior: 'smooth'});
    flash(el);
    return {ok: true, filled: fieldLabel(el), type: kind, value: fieldValue(el, kind), note: '只填写了内容，没有保存或提交'};
  }
  return {ok: false, error: 'field_not_found', message: `没有找到标签为「${params.label}」的输入项`, fields: describeFields(regionElement('').element).map(item => item.label).slice(0, 40)};
}

async function invoke(params) {
  const action = liveActions().find(item => item.name === params.name);
  if (!action) return {ok: false, error: 'action_not_found', message: `当前页面没有动作 ${params.name}`, actions: liveActions().map(item => item.name)};
  const result = await action.run(params.args || {});
  if (action.element) flash(action.element, 1800);
  return {ok: true, action: params.name, ...(result && typeof result === 'object' ? result : {result})};
}

export async function runPageRequest(request, {visibleChars = 4000} = {}) {
  const params = request?.params || {};
  // A task acts only on the page and object it was started on: elsewhere it would type into the wrong terminal.
  const here = currentScope();
  if (request?.scope?.key && request.scope.key !== here.key) {
    return {ok: false, error: 'page_left', here: here.label,
      message: `用户已离开发起这个任务的页面「${request.scope.label || request.scope.key}」，现在在「${here.label}」，页面操作没有执行。用后台工具完成；只能在页面上做的部分，告诉用户回到该页面后再做。`};
  }
  switch (request?.action) {
    case 'read': return read(params, {visibleChars});
    case 'locate': return locate(params);
    case 'fill': return fill(params);
    case 'invoke': return invoke(params);
    default: return {ok: false, error: 'unknown_action'};
  }
}
