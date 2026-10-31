// Stand-in for monaco-editor in component tests (happy-dom cannot run Monaco).
// A plain-text model with the subset of the editor API OpenBear's editors call;
// real Monaco typing stays in the browser tests.
const anything = () => new Proxy(function () {}, {get: (target, key) => key === Symbol.toPrimitive ? () => 0 : key === 'then' ? undefined : anything(), apply: () => anything()});
const disposable = () => ({dispose() {}});

function createModel(value = '', language = 'plaintext') {
  let text = String(value);
  const listeners = new Set();
  return {
    language,
    getValue: () => text,
    setValue(next) { text = String(next); for (const fn of listeners) fn({}); },
    getFullModelRange: () => ({startLineNumber: 1, startColumn: 1, endLineNumber: text.split('\n').length, endColumn: 1}),
    getLineCount: () => text.split('\n').length,
    getLineContent: line => text.split('\n')[line - 1] ?? '',
    onDidChangeContent(fn) { listeners.add(fn); return {dispose: () => listeners.delete(fn)}; },
    updateOptions() {}, dispose() { listeners.clear(); },
  };
}

function create(host, options = {}) {
  const model = options.model || createModel(options.value || '', options.language);
  const change = new Set(), blur = new Set();
  model.onDidChangeContent(() => { for (const fn of change) fn({}); });
  const node = host.ownerDocument.createElement('textarea');
  node.className = 'monaco-stub'; node.setAttribute('aria-label', options.ariaLabel || '');
  node.value = model.getValue();
  node.addEventListener('input', () => model.setValue(node.value));
  node.addEventListener('blur', () => { for (const fn of blur) fn({}); });
  host.append(node);
  return {
    getModel: () => model, getValue: () => model.getValue(), setValue: value => { model.setValue(value); node.value = value; },
    onDidChangeModelContent(fn) { change.add(fn); return {dispose: () => change.delete(fn)}; },
    onDidBlurEditorWidget(fn) { blur.add(fn); return {dispose: () => blur.delete(fn)}; },
    onDidFocusEditorWidget: disposable, onDidChangeCursorPosition: disposable, onKeyDown: disposable, onDidLayoutChange: disposable, onDidContentSizeChange: disposable,
    executeEdits(source, edits) { for (const edit of edits) model.setValue(edit.text); node.value = model.getValue(); return true; },
    updateOptions() {}, layout() {}, focus() { node.focus(); }, dispose() { node.remove(); },
    getAction: () => ({run() {}}), addCommand() {}, addAction: disposable, trigger() {}, getContentHeight: () => 0, getOption: () => 0, getDomNode: () => node, revealLine() {}, setPosition() {}, getPosition: () => ({lineNumber: 1, column: 1}),
  };
}

export const editor = {create, createModel, defineTheme() {}, setTheme() {}, setModelLanguage(model, language) { model.language = language; }, getModels: () => [], EditorOption: anything()};
export const languages = new Proxy({registerCompletionItemProvider: disposable, register() {}, setMonarchTokensProvider: disposable, setLanguageConfiguration: disposable}, {get: (t, k) => k in t ? t[k] : anything()});
export const KeyMod = anything(), KeyCode = anything(), MarkerSeverity = anything(), Uri = anything();
export class Range { constructor(a, b, c, d) { Object.assign(this, {startLineNumber: a, startColumn: b, endLineNumber: c, endColumn: d}); } }
export default {editor, languages, KeyMod, KeyCode, MarkerSeverity, Uri, Range};
