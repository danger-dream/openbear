<script setup>
import {computed, defineAsyncComponent, onBeforeUnmount, ref, shallowRef, toRaw, watch} from 'vue';
import {Api} from '../../api.js';
import {randomUuid} from '../../utils/randomUuid.js';
import './contextEditor/style.css';
import {assertDocument, callImpact, clone, deleteCall, deleteEntries, deleteReasoning, diffDocument, exportPackage, importPackage, isClaudeNative, localIssues, nativeCall, reasoningImpact, reconcileMessage, relations} from './contextEditor/operations.js';
import {canMoveBlock, messageSequence, moveBlock, replaceBlock} from './contextEditor/sequence.js';
const props = defineProps({conversationUuid: {type: String, default: ''}, busy: {type: Boolean, default: false}});
const emit = defineEmits(['created', 'saved', 'open-change']);
const CodeEditor = defineAsyncComponent(() => import('./contextEditor/CodeEditor.vue'));
const entityEditor = ref(null), entityError = ref(''), toolSearch = ref(''), diffId = ref('');
const opened = ref(false), state = shallowRef(null), loading = ref(false), pending = ref(''), error = ref(''), notice = ref('');
const selected = ref('system'), tab = ref('structured'), search = ref(''), checked = ref([]), raw = ref(''), rawError = ref('');
const dialog = ref(null), deleteMode = ref('paired'), thinkingScope = ref('suffix'), branchTitle = ref(''), importInput = ref(null);
const cache = new Map(); let generation = 0, disposed = false;
const editorShell = ref(null), compactViewport = ref(false), compactPanel = ref('document');
// Observe the actual space left by the mobile shell, not CSS media-query
// height (iOS can keep that unchanged while the keyboard shrinks the viewport).
watch(editorShell, (element, _, onCleanup) => {
  if (!element || typeof window === 'undefined') { compactViewport.value = false; return; }
  const sync = () => {
    const {width, height} = element.getBoundingClientRect();
    compactViewport.value = height > 0 && height < 500 && (width <= 760 || Boolean(window.matchMedia?.('(hover: none) and (pointer: coarse)').matches));
    if (!compactViewport.value) compactPanel.value = 'document';
  };
  const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(sync);
  observer?.observe(element);
  window.addEventListener('resize', sync);
  window.visualViewport?.addEventListener('resize', sync);
  sync();
  onCleanup(() => { observer?.disconnect(); window.removeEventListener('resize', sync); window.visualViewport?.removeEventListener('resize', sync); });
}, {flush: 'post'});
function switchCompactPanel(panel) { if (applyPendingRaw()) compactPanel.value = panel; }
const uuid = computed(() => props.conversationUuid && !props.conversationUuid.startsWith('local:') ? props.conversationUuid : '');
const doc = computed(() => state.value?.working);
const entry = computed(() => doc.value?.entries.find(e => e.entryId === selected.value));
const entryIndex = computed(() => doc.value?.entries.findIndex(e => e.entryId === selected.value) ?? -1);
const changes = computed(() => state.value ? diffDocument(state.value.baseline, state.value.working) : []);
const hints = computed(() => doc.value ? localIssues(doc.value) : []);
const pairs = computed(() => doc.value ? relations(doc.value).pairs : []);
const filtered = computed(() => (doc.value?.entries || []).filter(e => !search.value || JSON.stringify(e).toLowerCase().includes(search.value.toLowerCase())));
const previewReady = computed(() => Boolean(state.value?.preview && state.value.previewEpoch === state.value.epoch));
const savingContext = computed(() => ['create', 'apply'].includes(pending.value));
const saveReady = computed(() => !pending.value && !loading.value && !props.busy && state.value?.mode === 'compatible' && previewReady.value && state.value.preview.payload != null && !serverErrors.value.length && !hints.value.some(i => i.severity === 'error') && !state.value.latestSource);
const serverIssues = computed(() => previewReady.value ? state.value.preview.issues || [] : []);
const serverErrors = computed(() => serverIssues.value.filter(i => i.severity === 'error'));
const automaticRepair = computed(() => serverIssues.value.find(i => i.code === 'reasoning_cleanup'));
const payloadText = computed(() => previewReady.value && state.value.preview.payload != null ? JSON.stringify(state.value.preview.payload, null, 2) : '服务器未返回可预览的 payload');
const rawObject = computed(() => selected.value === 'system' ? {system: doc.value?.system} : selected.value === 'tools' ? doc.value?.tools : entry.value?.message);
const label = computed(() => selected.value === 'system' ? '系统提示词' : selected.value === 'tools' ? '工具定义' : `${entry.value?.message.role || '消息'} · 第 ${entryIndex.value + 1} 条`);
const toolRows = computed(() => (doc.value?.tools || []).map((tool, index) => ({tool, index})).filter(({tool}) => !toolSearch.value || `${tool.name} ${tool.description || ''}`.toLowerCase().includes(toolSearch.value.toLowerCase())));
const currentPairs = computed(() => pairs.value.filter(p => p.entryId === selected.value || p.results.includes(selected.value)));
const rawDirty = computed(() => raw.value !== JSON.stringify(rawObject.value ?? {}, null, 2));
const extraFields = computed(() => ['reasoning', 'signature', 'native_output_items'].filter(field => entry.value && Object.hasOwn(entry.value.message, field)));
const sequence = computed(() => messageSequence(entry.value?.message || {}));
const targetProtocol = computed(() => state.value?.models.find(m => m.model === state.value.targetModel)?.protocol || '');
const orderNote = computed(() => sequence.value.ordered
  ? `从上到下是已保存的原生块顺序，可逐块上移或下移。${targetProtocol.value === 'responses' || (['claude', 'anthropic'].includes(targetProtocol.value) && isClaudeNative(entry.value?.message.native_output_items)) ? '调整会写入实际请求；' : '切换协议不保证保留此顺序；'}编辑影响的历史思考、签名和密文会由框架自动处理。`
  : '此消息只保存了独立字段，未记录完整交错顺序；以下不是生成时间线。只能调整同一数组内的顺序，不能跨正文、思考、调用字段移动。');
const activeDiff = computed(() => changes.value.find(item => item.id === diffId.value) || changes.value[0]);
function snippet(message) {
  if (message.role === 'tool') return `${message.name || '工具结果'} · ${typeof message.content === 'string' ? message.content : '内容块'}`;
  if (message.tool_calls?.length) return message.tool_calls.map(c => c.name || '未命名调用').join(' · ');
  return typeof message.content === 'string' && message.content ? message.content : Array.isArray(message.content) ? `${message.content.length} 个内容块` : message.reasoning ? '思考内容' : '无正文';
}
function contentLanguage(value) { try { JSON.parse(value); return 'json'; } catch { return 'plaintext'; } }
const display = v => v === undefined ? '（不存在）' : typeof v === 'string' ? v : JSON.stringify(v, null, 2);
const failure = e => e?.response?.status === 409 ? `冲突（409）：${e?.response?.data?.message || e?.response?.data?.error || '源快照过期、会话运行中或草稿修订已变化'}。草稿仍保留；请导出并核对源会话，不会覆盖。` : String(e?.response?.data?.message || e?.response?.data?.error || e?.message || '请求失败');
function resetSelection() { selected.value = 'system'; tab.value = 'structured'; checked.value = []; search.value = ''; diffId.value = ''; refreshRaw(); }
function refreshRaw() { raw.value = JSON.stringify(rawObject.value ?? {}, null, 2); rawError.value = ''; }
function choose(id) { if (!applyPendingRaw()) return; selected.value = id; refreshRaw(); compactPanel.value = 'document'; }
function issueTarget(issue) {
  if (issue?.entryId && doc.value?.entries.some(e => e.entryId === issue.entryId)) return issue.entryId;
  if (/^system(?:\.|$)/.test(issue?.path || '')) return 'system';
  if (/^tools(?:\[|\.|$)/.test(issue?.path || '')) return 'tools';
  return '';
}
function inspectIssue(issue) { const target = issueTarget(issue); if (target) { choose(target); switchTab('structured'); } }
function applyPendingRaw() { return tab.value !== 'raw' || !rawDirty.value || saveRaw(); }
function switchTab(next) { if (!applyPendingRaw()) return; tab.value = next; if (next === 'raw') refreshRaw(); }
function setState(next) { state.value = next; if (uuid.value) cache.set(uuid.value, next); }
function hasDraftChanges(s, sourceModel = '') {
  return diffDocument(s.baseline, s.working).length > 0 || s.mode === 'raw'
    || Boolean(s.targetModel && s.targetModel !== (s.sourceModel || s.baseline.origin?.modelLabel || sourceModel || s.targetModel));
}
function editorState(result, draft = null) {
  const baseline = clone(draft?.baseline || result.baseline), working = clone(draft?.working || result.baseline);
  assertDocument(baseline); assertDocument(working);
  const sourceModel = result.targetModel || result.models?.[0]?.model || '';
  const snapshotToken = draft?.snapshotToken || result.snapshotToken;
  return {baseline, working, snapshotToken, revision: result.draft?.revision ?? draft?.revision ?? 0,
    mode: draft?.mode === 'raw' ? 'raw' : 'compatible', targetModel: draft?.targetModel || sourceModel, sourceModel,
    models: result.models || [], epoch: 0, savedEpoch: 0, preview: null, previewEpoch: -1, undo: [], redo: [],
    latestSource: snapshotToken !== result.snapshotToken ? result : null};
}
function showCached(key) {
  state.value = cache.get(key) || null; error.value = ''; notice.value = ''; pending.value = ''; dialog.value = null; entityEditor.value = null;
}
async function load() {
  const key = uuid.value, stamp = ++generation;
  showCached(key);
  if (!key) { loading.value = false; resetSelection(); return; }
  loading.value = true;
  try {
    const result = await Api.contextEditor(key);
    if (disposed || stamp !== generation || uuid.value !== key) return;
    if (!result?.ok) throw Error('服务器未返回上下文');
    assertDocument(result.baseline);
    // Re-read the cache after the request: an editor blur or an in-flight save
    // may have committed a newer local draft while this read was pending.
    const current = cache.get(key);
    if (current && (current.snapshotToken === result.snapshotToken || hasDraftChanges(current))) {
      const changed = current.snapshotToken !== result.snapshotToken;
      setState({...current, models: result.models || [], latestSource: changed ? result : null,
        preview: changed ? null : current.preview, previewEpoch: changed ? -1 : current.previewEpoch});
    } else {
      // A saved but untouched old snapshot must not hide later conversation turns.
      // An intentionally edited saved draft remains intact until the user replaces it.
      const draft = !current && result.draft && (result.draft.snapshotToken === result.snapshotToken || hasDraftChanges(result.draft, result.targetModel)) ? result.draft : null;
      setState(editorState(result, draft));
    }
    resetSelection();
  } catch (e) { if (stamp === generation && uuid.value === key) error.value = `${failure(e)}${state.value ? '；未能核对最新上下文，当前仍保留原编辑稿。' : ''}`; }
  finally { if (stamp === generation) loading.value = false; }
}
function askLatestSource() { if (state.value?.latestSource) dialog.value = {kind: 'refresh', impact: []}; }
function useLatestSource() {
  const result = state.value?.latestSource;
  if (!result) return;
  setState(editorState(result)); resetSelection(); error.value = ''; notice.value = '已载入最新上下文；原会话及服务器旧草稿未改动';
}

// A read assembles the whole context on the server; only an opened editor needs it.
// Retire the previous conversation's read so opening this one starts its own.
watch(uuid, () => { opened.value = false; generation++; loading.value = false; showCached(uuid.value); resetSelection(); });
watch(opened, value => emit('open-change', value), {flush: 'sync'});
onBeforeUnmount(() => { disposed = true; generation++; emit('open-change', false); });
function open() { if (!uuid.value) return; if (props.busy) { notice.value = '运行中无法编辑上下文'; return; } opened.value = true; if (!loading.value) load(); }
function close() { if (!applyPendingRaw()) return; opened.value = false; dialog.value = null; entityEditor.value = null; }
defineExpose({open, close});
function change(next) {
  if (savingContext.value) throw Error('上下文保存中，暂不能继续改动');
  assertDocument(next);
  const s = state.value;
  setState({...s, working: next, undo: [...s.undo, clone(s.working)], redo: [], epoch: s.epoch + 1, preview: null, previewEpoch: -1});
  if (!['system','tools'].includes(selected.value) && !next.entries.some(e => e.entryId === selected.value)) { selected.value = 'system'; refreshRaw(); }
  error.value = '';
}
function mutate(fn) { try { const next = clone(doc.value); fn(next); change(next); return true; } catch (e) { error.value = e.message; return false; } }
function editMessage(fn, id = selected.value) { return mutate(d => { const item = d.entries.find(e => e.entryId === id); if (!item) throw Error('消息已不存在'); const original = clone(item.message); fn(item.message); item.message = reconcileMessage(original, item.message); }); }
function commitText({scope, id, field, value}) {
  if (scope !== uuid.value || !doc.value) return;
  if (id === 'system') { if (doc.value.system !== value) mutate(d => { d.system = value; }); }
  else if (doc.value.entries.some(e => e.entryId === id) && doc.value.entries.find(e => e.entryId === id).message[field] !== value) editMessage(m => { m[field] = value; }, id);
}
function setField(key, value) { selected.value === 'system' ? mutate(d => { d.system = value; }) : editMessage(m => { m[key] = value; }); }
function setting(key, value) { const s = state.value; if (savingContext.value) return; if (s && s[key] !== value) setState({...s, [key]: value, epoch: s.epoch + 1, preview: null, previewEpoch: -1}); }
function history(direction) { if (!applyPendingRaw()) return; const s = state.value; if (savingContext.value || !s?.[direction].length) return; const other = direction === 'undo' ? 'redo' : 'undo'; setState({...s, [direction]: s[direction].slice(0,-1), [other]: [...s[other], clone(s.working)], working: clone(s[direction].at(-1)), epoch: s.epoch + 1, preview: null, previewEpoch: -1}); if (!['system','tools'].includes(selected.value) && !doc.value.entries.some(e => e.entryId === selected.value)) selected.value = 'system'; checked.value = checked.value.filter(id => doc.value.entries.some(e => e.entryId === id)); refreshRaw(); }
function addEntry(role) { if (!applyPendingRaw()) return; const id = `edited-${randomUuid()}`; if (mutate(d => { const i = d.entries.findIndex(e => e.entryId === selected.value); d.entries.splice(i < 0 ? d.entries.length : i + 1, 0, {entryId: id, message: role === 'tool' ? {role, content: '', name: '', tool_call_id: ''} : {role, content: ''}}); })) choose(id); }
function addTool() { openEntity('tool', -1); }
function addCall() { openEntity('call', -1); }
function move(offset) { if (!applyPendingRaw()) return; const i = entryIndex.value; if (i >= 0 && i + offset >= 0 && i + offset < doc.value.entries.length) mutate(d => { [d.entries[i], d.entries[i+offset]] = [d.entries[i+offset], d.entries[i]]; }); }
function askCall(call) { try { deleteMode.value = 'paired'; dialog.value = {kind: 'call', id: selected.value, callId: call.id, impact: callImpact(doc.value, selected.value, call.id)}; } catch (e) { error.value = e.message; } }
function askThinking() { const impact = reasoningImpact(doc.value, selected.value); if (!impact.length) return; thinkingScope.value = 'suffix'; dialog.value = {kind: 'thinking', id: selected.value, impact}; }
function askEntries(ids) { if (!applyPendingRaw()) return; const affected = new Set(ids), impact = []; for (const p of pairs.value) { if (affected.has(p.entryId)) p.results.forEach(id => affected.add(id)); else if (p.results.some(id => affected.has(id))) impact.push(`${p.entryId}.tool_calls[${p.call.id}]`); } dialog.value = {kind: 'entries', ids, impact: [...affected].map(id => `${id}.message`).concat(impact)}; }
function confirmDelete() {
  const action = dialog.value;
  if (action.kind === 'apply') { dialog.value = null; return saveOriginal(); }
  if (action.kind === 'refresh') useLatestSource();
  else if (action.kind === 'call') { mutate(d => { d.entries = deleteCall(d, action.id, action.callId, deleteMode.value === 'only').entries; }); if (deleteMode.value === 'only') setting('mode', 'raw'); }
  else if (action.kind === 'thinking') mutate(d => { d.entries = deleteReasoning(d, action.id, thinkingScope.value === 'suffix').entries; });
  else if (action.kind === 'entries') mutate(d => { d.entries = deleteEntries(d, action.ids).entries; checked.value = []; });
  else if (action.kind === 'tool') mutate(d => { d.tools.splice(action.index, 1); });
  dialog.value = null;
}
function saveRaw() { try { if (savingContext.value) throw Error('上下文保存中，暂不能修改'); const parsed = JSON.parse(raw.value), next = clone(doc.value); if (selected.value === 'system') { if (typeof parsed?.system !== 'string') throw Error('system 必须是字符串'); next.system = parsed.system; } else if (selected.value === 'tools') next.tools = parsed; else next.entries.find(e => e.entryId === selected.value).message = reconcileMessage(entry.value.message, parsed); assertDocument(next); change(next); refreshRaw(); return true; } catch (e) { rawError.value = e.message; return false; } }
function restore(changeItem) { mutate(d => { const b = state.value.baseline; if (!changeItem) { Object.assign(d, clone(b)); return; } const id = changeItem.id; if (id === 'system' || id === 'tools') d[id] = clone(b[id]); else if (id === 'order') { const order = new Map(b.entries.map((e,i) => [e.entryId,i])); d.entries.sort((a,c) => (order.get(a.entryId) ?? Infinity) - (order.get(c.entryId) ?? Infinity)); } else { const i = d.entries.findIndex(e => e.entryId === id), original = b.entries.find(e => e.entryId === id); if (i >= 0) d.entries.splice(i, 1); if (original) d.entries.splice(Math.min(b.entries.indexOf(original), d.entries.length), 0, clone(original)); } }); }
function requestData(s) { return {baseline: clone(s.baseline), working: clone(s.working), mode: s.mode, targetModel: s.targetModel}; }
async function preview() {
  if (!applyPendingRaw()) return;
  const key = uuid.value, stamp = generation, s = state.value;
  if (!s || pending.value) return;
  pending.value = 'preview'; error.value = '';
  try { const result = await Api.previewContextEditor(key, requestData(s));
    if (disposed || stamp !== generation || uuid.value !== key || state.value?.epoch !== s.epoch) return;
    if (!result?.ok) throw Error('服务器预览失败');
    setState({...state.value, preview: result, previewEpoch: s.epoch}); tab.value = 'preview';
  } catch (e) { if (stamp === generation && uuid.value === key) error.value = failure(e); }
  finally { if (stamp === generation) pending.value = ''; }
}
async function saveDraft() {
  if (!applyPendingRaw()) return;
  const key = uuid.value, stamp = generation, s = state.value;
  if (!s || pending.value) return;
  pending.value = 'save'; error.value = '';
  try { const result = await Api.saveContextEditorDraft(key, {...requestData(s), snapshotToken: s.snapshotToken, revision: s.revision});
    if (disposed) return;
    if (!result?.ok || !result.draft) throw Error('服务器未返回草稿修订');
    if (stamp !== generation || uuid.value !== key) {
      // Only update the originating conversation's optimistic revision, never the currently visible conversation.
      const old = cache.get(key);
      if (old?.revision === s.revision && old?.snapshotToken === s.snapshotToken) cache.set(key, {...old, revision: result.draft.revision, savedEpoch: s.epoch});
      return;
    }
    setState({...state.value, revision: result.draft.revision, savedEpoch: s.epoch});
    notice.value = state.value.epoch === s.epoch ? '服务器草稿已保存' : '请求期间有新的编辑；新修改尚未保存';
  } catch (e) { if (stamp === generation && uuid.value === key) error.value = failure(e); }
  finally { if (stamp === generation) pending.value = ''; }
}
function validateOriginalSave() {
  if (!applyPendingRaw() || !state.value || pending.value) return false;
  if (props.busy) { error.value = '会话运行中不能保存到原会话；草稿保留'; return false; }
  if (state.value.latestSource) { error.value = '源会话已有新内容，请先加载最新上下文；当前编辑稿可保存或导出'; return false; }
  if (state.value.mode !== 'compatible') { error.value = '原始实验稿只可保存草稿或导出，不可应用到会话'; return false; }
  if (!previewReady.value) { error.value = '编辑或目标变化后须重新运行服务器预览'; return false; }
  if (state.value.preview.payload == null || serverErrors.value.length || hints.value.some(i => i.severity === 'error')) { error.value = '请先处理结构错误并生成实际请求载荷'; return false; }
  return true;
}
function askSaveOriginal() {
  if (validateOriginalSave()) dialog.value = {kind: 'apply', impact: []};
}
async function saveOriginal() {
  if (!validateOriginalSave()) return;
  const key = uuid.value, stamp = generation, s = state.value;
  pending.value = 'apply'; error.value = '';
  try {
    const result = await Api.saveContextEditor(key, {...requestData(s), snapshotToken: s.snapshotToken, revision: s.revision});
    if (disposed) return;
    if (!result?.ok || !result.baseline || !result.snapshotToken) throw Error('服务器未返回已保存的上下文');
    const next = editorState({...result, models: s.models});
    if (stamp !== generation || uuid.value !== key) {
      const current = cache.get(key);
      if (current?.snapshotToken === s.snapshotToken && current.epoch === s.epoch) cache.set(key, next);
      else if (current) cache.set(key, {...current, latestSource: {...result, models: s.models}, preview: null, previewEpoch: -1});
      return;
    }
    setState(next); resetSelection();
    notice.value = '已保存到原会话，下一次消息使用新上下文；原始聊天与执行记录未改动';
    emit('saved', result.conversation);
  } catch (e) { if (stamp === generation && uuid.value === key) error.value = failure(e); }
  finally { if (stamp === generation) pending.value = ''; }
}
async function createBranch() {
  if (!applyPendingRaw()) return;
  const key = uuid.value, stamp = generation, s = state.value;
  if (!s || pending.value) return;
  if (props.busy) { error.value = '会话运行中不能创建分支；草稿保留'; return; }
  if (s.latestSource) { error.value = '源会话已有新内容，请先加载最新上下文；当前编辑稿可保存或导出'; return; }
  if (s.mode !== 'compatible') { error.value = '原始实验稿只可保存或导出，不可执行'; return; }
  if (!previewReady.value) { error.value = '编辑或目标变化后须重新运行服务器预览'; return; }
  if (s.preview.payload == null) { error.value = '服务器未生成实际请求载荷，不能创建分支'; return; }
  if (serverErrors.value.length || hints.value.some(i => i.severity === 'error')) { error.value = '请先处理服务端或本地结构错误'; return; }
  if (!branchTitle.value.trim()) { error.value = '请输入分支标题'; return; }
  pending.value = 'create'; error.value = '';
  try { const result = await Api.createContextEditorBranch(key, {...requestData(s), snapshotToken: s.snapshotToken, title: branchTitle.value.trim()});
    if (disposed || stamp !== generation || uuid.value !== key) return;
    if (!result?.ok || !result.conversation) throw Error('服务器未返回分支会话');
    close(); emit('created', result.conversation);
  } catch (e) { if (stamp === generation && uuid.value === key) error.value = failure(e); }
  finally { if (stamp === generation) pending.value = ''; }
}
function download() { if (!state.value || !applyPendingRaw()) return; const blob = new Blob([JSON.stringify(exportPackage(state.value.baseline, doc.value, state.value.mode, state.value.targetModel), null, 2)], {type:'application/json;charset=utf-8'}); const url = URL.createObjectURL(blob), link = document.createElement('a'); link.href = url; link.download = 'openbear-context-edit.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
async function upload(event) {
  const file = event.target.files?.[0], key = uuid.value, stamp = generation;
  event.target.value = '';
  if (!file || savingContext.value) return;
  try {
    const pkg = importPackage(JSON.parse(await file.text()));
    if (disposed || stamp !== generation || uuid.value !== key || savingContext.value) return;
    const s = state.value; if (!s) throw Error('请先载入会话快照');
    setState({...s, baseline: pkg.baseline, working: pkg.working, mode: pkg.mode, targetModel: pkg.targetModel || s.targetModel, epoch: s.epoch + 1, preview: null, previewEpoch: -1, undo: [], redo: []});
    resetSelection(); error.value = ''; notice.value = '完整基线和编辑稿已导入；源快照仍由服务器检查';
  } catch (e) { if (stamp === generation && uuid.value === key) error.value = `导入失败：${e.message}`; }
}

function revealBody() {
  if (sequence.value.ordered) {
    if (editMessage(m => { m.native_output_items.push(isClaudeNative(m.native_output_items) ? {type: 'text', text: ''} : {type: 'message', role: 'assistant', content: [{type: 'output_text', text: ''}]}); })) openBlock(sequence.value.rows.at(-1));
  } else { if (entry.value.message.content == null) setField('content', ''); openEntity('field', -1, 'content'); }
}
function shiftBlock(row, offset) {
  mutate(d => { const e = d.entries.find(e => e.entryId === selected.value); e.message = moveBlock(e.message, row.key, offset); });
}
function openBlock(row) {
  if (row.source === 'tool_calls' || ['function_call', 'tool_use'].includes(row.type) && entry.value.message.tool_calls?.some(c => c.id === row.callId)) {
    openEntity('call', entry.value.message.tool_calls.findIndex(c => c.id === row.callId)); return;
  }
  if (row.source !== 'native_output_items') { openEntity('field', -1, row.source); return; }
  const data = clone(row.value);
  entityError.value = '';
  const textIndexes = data.type === 'message' ? (data.content || []).flatMap((b,i) => ['text','output_text'].includes(b.type) ? [i] : []) : [];
  const blockTextIndex = textIndexes.length === 1 ? textIndexes[0] : -1, blockTextField = data.type === 'text';
  entityEditor.value = {kind: 'field', field: `内容块 ${row.index + 1} · ${row.label}`, block: {...row, value: undefined}, blockTextIndex, blockTextField, entryId: selected.value, scope: uuid.value, data, view: 'fields', raw: JSON.stringify(data, null, 2), text: blockTextField ? data.text || '' : blockTextIndex >= 0 ? data.content[blockTextIndex].text || '' : JSON.stringify(data, null, 2), json: !blockTextField && blockTextIndex < 0};
}
function openEntity(kind, index = -1, field = '') {
  if (!applyPendingRaw()) return;
  const value = kind === 'tool' ? (index < 0 ? {name: '', description: '', parameters: {type: 'object', properties: {}}} : doc.value.tools[index])
    : kind === 'call' ? (index < 0 ? {id: `call_${randomUuid()}`, name: '', arguments: '{}'} : entry.value.message.tool_calls[index])
    : entry.value.message[field];
  const data = clone(value);
  entityError.value = '';
  entityEditor.value = {kind, index, field, entryId: selected.value, scope: uuid.value, data,
    view: 'fields', raw: JSON.stringify(data, null, 2),
    text: kind === 'tool' ? JSON.stringify(data.parameters ?? {}, null, 2) : kind === 'call' ? data.arguments : typeof data === 'string' ? data : JSON.stringify(data, null, 2),
    json: kind !== 'field' || typeof data !== 'string'};
}
function entityValue(draft) {
  if (draft.view === 'raw') return JSON.parse(draft.raw);
  if (draft.kind === 'field') {
    if (draft.blockTextField) return {...clone(toRaw(draft.data)), text: draft.text};
    if (draft.blockTextIndex >= 0) { const value = clone(toRaw(draft.data)); value.content[draft.blockTextIndex].text = draft.text; return value; }
    return draft.json ? JSON.parse(draft.text) : draft.text;
  }
  const value = clone(toRaw(draft.data));
  if (draft.kind === 'tool') value.parameters = JSON.parse(draft.text);
  else value.arguments = draft.text;
  return value;
}
function entityView(view) {
  const draft = entityEditor.value;
  if (draft.view === view) return;
  try {
    const value = entityValue(draft);
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('定义必须是 JSON 对象');
    draft.data = value; draft.raw = JSON.stringify(value, null, 2);
    draft.text = draft.kind === 'tool' ? JSON.stringify(value.parameters ?? {}, null, 2) : value.arguments;
    draft.view = view; entityError.value = '';
  } catch (e) { entityError.value = e.message; }
}
function saveEntity() {
  const draft = entityEditor.value;
  if (!draft || draft.scope !== uuid.value) return;
  try {
    const value = entityValue(draft);
    let saved;
    if (draft.kind === 'tool') {
      if (!value || typeof value.name !== 'string' || !value.name.trim()) throw Error('请输入工具名称');
      if (!value.parameters || typeof value.parameters !== 'object' || Array.isArray(value.parameters)) throw Error('参数必须是 JSON 对象');
      saved = mutate(d => { if (draft.index < 0) d.tools.push(value); else d.tools[draft.index] = value; });
    } else if (draft.kind === 'call') {
      if (!value || typeof value.name !== 'string' || typeof value.id !== 'string' || typeof value.arguments !== 'string') throw Error('调用需要 name、id 和字符串 arguments');
      saved = editMessage(m => {
        if (draft.index < 0 && messageSequence(m).ordered) {
          m.native_output_items.push(nativeCall(value, isClaudeNative(m.native_output_items)));
          m.tool_calls ||= []; m.tool_calls.push(value);
        }
        else { m.tool_calls ||= []; if (draft.index < 0) m.tool_calls.push(value); else m.tool_calls[draft.index] = value; }
      }, draft.entryId);
    } else if (draft.block) saved = mutate(d => {
      const item = d.entries.find(e => e.entryId === draft.entryId);
      if (!item) throw Error('消息已不存在');
      item.message = replaceBlock(item.message, draft.block, value);
    });
    else saved = editMessage(m => { m[draft.field] = value; }, draft.entryId);
    if (!saved) { entityError.value = error.value; return; }
    entityEditor.value = null;
    if (draft.kind === 'tool') { selected.value = 'tools'; tab.value = 'structured'; }
    refreshRaw();
  } catch (e) { entityError.value = e.message; }
}
</script>
<template>
  <el-tooltip v-if="uuid && !opened" :content="busy ? '会话运行中无法编辑上下文' : '编辑上下文'" placement="left" :show-after="260">
    <button type="button" class="context-editor-entry" aria-label="编辑上下文" :aria-disabled="busy ? 'true' : 'false'" @click="open"><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M11 21H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v3M13 3v5h5M8 11h4M8 15h2M13 21l1-4 6-6a1.4 1.4 0 0 1 2 2l-6 6-3 2ZM18.5 12.5l2 2"/></svg></button>
  </el-tooltip>
  <section v-if="opened" ref="editorShell" class="context-editor ce-surface" :class="{'is-compact': compactViewport}" :data-compact-panel="compactPanel" aria-label="上下文编辑器" :aria-busy="!!pending || loading">
    <nav v-if="compactViewport" class="ce-compact-toolbar" aria-label="短屏幕编辑操作">
      <button type="button" @click="close">返回</button>
      <button type="button" :aria-pressed="compactPanel === 'outline'" @click="switchCompactPanel('outline')">目录</button>
      <button type="button" :aria-pressed="compactPanel === 'document'" @click="switchCompactPanel('document')">正文</button>
      <button type="button" :aria-pressed="compactPanel === 'controls'" @click="switchCompactPanel('controls')">操作</button>
      <button type="button" class="ce-primary" :disabled="!doc || !!pending || loading" @click="saveDraft">{{ pending === 'save' ? '保存中…' : '保存草稿' }}</button>
    </nav>
    <div class="ce-content">
    <header class="ce-heading">
      <div><h2>上下文编辑</h2><small>可保存副本或应用到原会话，不改原始聊天记录</small></div>
      <div class="ce-actions"><button type="button" @click="close">← 返回对话</button><button type="button" @click="importInput?.click()">导入</button><button type="button" :disabled="!doc" @click="download">导出完整包</button><input ref="importInput" type="file" accept=".json,application/json" hidden @change="upload"></div>
    </header>
    <div v-if="loading" class="ce-placeholder">正在读取会话上下文…</div>
    <div v-else-if="!doc" class="ce-placeholder">{{ error || '没有可编辑的上下文' }} <button type="button" @click="load">重试</button></div>
    <template v-else>
      <div v-if="state.latestSource" class="ce-source-notice" role="status"><span>源会话已有新内容（{{ state.latestSource.baseline.entries.length }} 条消息），当前保留的是旧快照上的编辑稿。</span><button type="button" :disabled="!!pending" @click="askLatestSource">加载最新上下文</button></div>
      <div class="ce-request">
        <label>目标模型 <select :value="state.targetModel" @change="setting('targetModel', $event.target.value)"><option v-for="model in state.models" :key="model.model" :value="model.model">{{ model.label }} · {{ model.protocol }}</option></select></label>
        <label>模式 <select :value="state.mode" @change="setting('mode', $event.target.value)"><option value="compatible">兼容续聊</option><option value="raw">原始实验（只保存）</option></select></label>
        <details class="ce-snapshot-summary"><summary>冻结快照 · {{ doc.entries.length }} 条消息</summary><p>保存到会话后冻结系统提示、工具定义及已有记忆；不会自动注入新的任务记忆；仍遵循会话压缩设置</p></details>
      </div>
      <div class="ce-workspace">
        <aside class="ce-outline" :class="{'has-selection': checked.length > 0}" aria-label="上下文结构">
          <div class="ce-outline-heading"><strong>上下文结构</strong><small>{{ doc.entries.length }} 条消息</small></div>
          <input v-model="search" class="ce-search" aria-label="搜索上下文" type="search" placeholder="搜索消息内容或字段…">
          <div class="ce-actions ce-add-actions"><button type="button" @click="addEntry('user')">＋ 用户</button><button type="button" @click="addEntry('assistant')">＋ 助手</button><button type="button" @click="addEntry('tool')">＋ 结果</button></div>
          <div class="ce-outline-scroll">
            <button type="button" class="ce-outline-link" :class="{active: selected === 'system'}" :aria-current="selected === 'system' ? 'page' : undefined" @click="choose('system')"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 3H6v18h12V7l-4-4Zm0 0v5h4M9 12h6M9 16h6"/></svg><span>系统提示词</span><span class="ce-chevron">›</span></button>
            <button type="button" class="ce-outline-link" :class="{active: selected === 'tools'}" :aria-current="selected === 'tools' ? 'page' : undefined" @click="choose('tools')"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 5-6 7 6 7m8-14 6 7-6 7M14 3l-4 18"/></svg><span>工具定义</span><small>{{ doc.tools.length }}</small><span class="ce-chevron">›</span></button>
            <div class="ce-group">消息序列 <span v-if="search">· {{ filtered.length }} 条匹配</span></div>
            <div v-for="item in filtered" :key="item.entryId" class="ce-tree-item" :class="{active: selected === item.entryId}">
              <input v-model="checked" type="checkbox" :value="item.entryId" :aria-label="`选择 ${item.entryId}`">
              <button type="button" :aria-current="selected === item.entryId ? 'page' : undefined" @click="choose(item.entryId)"><span class="ce-message-label"><span class="ce-role" :class="item.message.role">{{ item.message.role }}</span><small>#{{ doc.entries.indexOf(item) + 1 }}</small></span><small class="ce-snippet" :title="snippet(item.message)">{{ snippet(item.message) }}</small></button>
            </div>
            <p v-if="search && !filtered.length" class="ce-empty">没有匹配的消息</p>
          </div>
          <div v-if="checked.length" class="ce-selection"><span>已选 {{ checked.length }} 条</span><button type="button" @click="checked = []">取消</button><button type="button" class="ce-danger" @click="askEntries(checked)">删除…</button></div>
        </aside>
        <main class="ce-document" aria-label="正文编辑">
          <div class="ce-document-title"><strong>{{ label }}</strong><div v-if="entry" class="ce-actions"><button type="button" :disabled="entryIndex === 0" @click="move(-1)">上移</button><button type="button" :disabled="entryIndex === doc.entries.length - 1" @click="move(1)">下移</button><button type="button" class="ce-danger" @click="askEntries([selected])">删除消息</button></div><small v-else>{{ selected === 'system' ? `${doc.system.length.toLocaleString()} 字符` : `${doc.tools.length} 个定义` }}</small></div>
          <nav class="ce-tabs" aria-label="编辑视图"><button v-for="item in [['structured','结构化'],['raw','完整 JSON'],['diff','差异'],['preview','请求预览']]" :key="item[0]" type="button" :class="{active: tab === item[0]}" :aria-pressed="tab === item[0]" @click="switchTab(item[0])">{{ item[1] }}<small v-if="item[0] === 'diff' && changes.length">{{ changes.length }}</small></button></nav>
          <div class="ce-document-scroll">
            <template v-if="tab === 'structured'">
              <template v-if="selected === 'system'">
                <div class="ce-toolbar"><span>系统正文</span><small>离开编辑区自动应用到编辑稿 · 不写全局模板</small></div>
                <div class="ce-editor-fill"><CodeEditor :key="`${uuid}-system`" :model-value="doc.system" language="markdown" label="系统提示词正文" :identity="{scope: uuid, id: 'system', field: 'system'}" @change="commitText" /></div>
              </template>
              <template v-else-if="selected === 'tools'">
                <div class="ce-toolbar ce-tools-toolbar"><input v-model="toolSearch" type="search" aria-label="筛选工具定义" placeholder="筛选工具名称或说明…"><button type="button" @click="addTool">＋ 添加工具</button></div>
                <div class="ce-table-wrap ce-table-fill"><table class="ce-table ce-tools-table"><thead><tr><th>工具名称</th><th>说明</th><th class="ce-count">参数</th><th class="ce-row-actions">操作</th></tr></thead><tbody>
                  <tr v-for="{tool,index} in toolRows" :key="index"><td><button type="button" class="ce-name-link" @click="openEntity('tool',index)">{{ tool.name || '未命名工具' }}</button></td><td><span class="ce-ellipsis" :title="tool.description">{{ tool.description || '—' }}</span></td><td class="ce-count">{{ Object.keys(tool.parameters?.properties || {}).length }}</td><td class="ce-row-actions"><button type="button" :aria-label="`编辑 ${tool.name}`" @click="openEntity('tool',index)">编辑</button><button type="button" class="ce-danger" :aria-label="`删除 ${tool.name}`" @click="dialog = {kind:'tool',index,impact:[`tools[${index}] ${tool.name}`]}">删除</button></td></tr>
                </tbody></table><div v-if="!toolRows.length" class="ce-empty">{{ toolSearch ? '没有匹配的工具' : '还没有工具定义' }}</div></div>
                <p class="ce-help">这里只修改模型可见定义，不新增服务器执行权限。</p>
              </template>
              <template v-else-if="entry">
                <div class="ce-message-meta"><label>角色<select :value="entry.message.role" @change="setField('role',$event.target.value)"><option value="user">user</option><option value="assistant">assistant</option><option value="tool">tool</option></select></label><template v-if="entry.message.role === 'tool'"><label>工具名称<input :value="entry.message.name || ''" @change="setField('name',$event.target.value)"></label><label class="ce-grow">对应调用 ID<input :value="entry.message.tool_call_id || ''" @change="setField('tool_call_id',$event.target.value)"></label></template></div>
                <div v-if="entry.message.role === 'tool' && currentPairs.length" class="ce-related"><span>关联调用</span><button v-for="pair in currentPairs" :key="pair.entryId" type="button" @click="choose(pair.entryId)">{{ pair.call.name }} · 第 {{ doc.entries.findIndex(e => e.entryId === pair.entryId) + 1 }} 条消息 ↗</button></div>
                <section v-if="entry.message.role === 'assistant'" class="ce-message-section">
                  <div class="ce-toolbar"><strong>{{ sequence.ordered ? '内容块顺序' : '消息内容 · 顺序未记录' }} <small>{{ sequence.rows.length }} 项</small></strong><div class="ce-actions"><button type="button" @click="revealBody">＋ 正文块</button><button type="button" @click="addCall">＋ 工具调用</button></div></div>
                  <div class="ce-sequence-scroll">
                    <p class="ce-order-note">{{ orderNote }}</p>
                    <ol class="ce-sequence" aria-label="消息内容块">
                      <li v-for="(block,i) in sequence.rows" :key="block.key" class="ce-block" :data-block-type="block.type">
                        <span class="ce-block-number">{{ sequence.ordered ? String(i + 1).padStart(2, '0') : '·' }}</span>
                        <div class="ce-block-content">
                          <div class="ce-block-heading"><strong>{{ block.label }}</strong><small>{{ block.source }}{{ block.index != null ? `[${block.index}]` : '' }}</small></div>
                          <p class="ce-block-preview">{{ block.preview.slice(0, 420) || '（空内容）' }}</p>
                          <small v-if="block.callId" class="ce-call-id">{{ block.callId }}</small>
                          <div v-if="block.callId" class="ce-block-results"><span>返回结果：</span><template v-for="pair in currentPairs.filter(p => p.call.id === block.callId)" :key="pair.call.id"><button v-for="id in pair.results" :key="id" type="button" class="ce-link" @click="choose(id)">第 {{ doc.entries.findIndex(e => e.entryId === id) + 1 }} 条消息 ↗</button><span v-if="!pair.results.length">无关联结果</span></template></div>
                        </div>
                        <div class="ce-block-actions"><button type="button" :aria-label="`上移第 ${i+1} 项`" :disabled="!canMoveBlock(entry.message,block.key,-1)" @click="shiftBlock(block,-1)">↑</button><button type="button" :aria-label="`下移第 ${i+1} 项`" :disabled="!canMoveBlock(entry.message,block.key,1)" @click="shiftBlock(block,1)">↓</button><button type="button" @click="openBlock(block)">编辑</button><button v-if="block.callId && entry.message.tool_calls?.some(c => c.id === block.callId)" type="button" class="ce-danger" @click="askCall(entry.message.tool_calls.find(c => c.id === block.callId))">删除…</button></div>
                      </li>
                    </ol>
                    <div v-if="!sequence.rows.length" class="ce-empty">此消息没有内容，可添加正文或调用。</div>
                    <details v-if="extraFields.length" class="ce-sequence-metadata"><summary>附加字段与原生数据 · {{ extraFields.length }}</summary><p class="ce-help">reasoning / signature 是附加字段，不代表独立的先后步骤；签名不会重新生成。</p><div v-for="field in extraFields" :key="field" class="ce-field-row"><code>{{ field }}</code><button type="button" @click="openEntity('field',-1,field)">查看 / 编辑</button></div><button type="button" class="ce-danger" @click="askThinking">联动删除思考…</button></details>
                  </div>
                </section>
                <template v-else>
                  <section v-if="entry.message.content != null" class="ce-body-section">
                    <div class="ce-toolbar"><strong>{{ entry.message.role === 'tool' ? '工具结果' : '消息正文' }}</strong><small v-if="typeof entry.message.content === 'string'">{{ entry.message.content.length.toLocaleString() }} 字符 · 离开编辑区自动应用</small><button v-else type="button" @click="openEntity('field',-1,'content')">编辑内容块</button></div>
                    <div class="ce-body-editor"><CodeEditor :key="`${uuid}-${selected}-content`" :model-value="display(entry.message.content)" :language="typeof entry.message.content !== 'string' ? 'json' : entry.message.role === 'tool' ? contentLanguage(entry.message.content) : 'markdown'" :readonly="typeof entry.message.content !== 'string'" label="消息正文" :identity="{scope: uuid, id: selected, field: 'content'}" @change="commitText" /></div>
                  </section>
                  <div v-else class="ce-empty-body"><span>此消息没有文本正文</span><button type="button" @click="revealBody">＋ 添加正文</button></div>
                </template>
              </template>
            </template>
            <template v-else-if="tab === 'raw'">
              <div class="ce-toolbar"><small>保留全部字段 · 行号、括号配色与层级折叠</small><button type="button" :disabled="!rawDirty" @click="saveRaw">应用 JSON</button></div>
              <div class="ce-editor-fill"><CodeEditor :key="`${uuid}-${selected}-raw`" v-model="raw" label="完整原始 JSON" /></div>
              <p v-if="rawError" class="ce-error" role="alert">{{ rawError }}</p>
            </template>
            <template v-else-if="tab === 'diff'">
              <div class="ce-toolbar"><span>{{ changes.length }} 项改动 · 基线 → 编辑稿</span><button type="button" :disabled="!changes.length" @click="restore(null)">全部还原</button></div>
              <div v-if="!changes.length" class="ce-empty">尚未修改上下文</div>
              <template v-if="activeDiff">
                <div class="ce-toolbar ce-diff-picker"><select :value="activeDiff.id" @change="diffId = $event.target.value" aria-label="选择差异项"><option v-for="item in changes" :key="item.id" :value="item.id">{{ item.id }} · {{ item.before === undefined ? '新增' : item.after === undefined ? '删除' : '修改' }}</option></select><button type="button" @click="restore(activeDiff)">还原此项</button></div>
                <div class="ce-diff-panes"><div><small>原始内容</small><div class="ce-diff-editor"><CodeEditor :model-value="display(activeDiff.before)" :language="typeof activeDiff.before === 'string' ? 'plaintext' : 'json'" readonly label="原始内容" /></div></div><div><small>编辑后</small><div class="ce-diff-editor"><CodeEditor :model-value="display(activeDiff.after)" :language="typeof activeDiff.after === 'string' ? 'plaintext' : 'json'" readonly label="编辑后内容" /></div></div></div>
              </template>
            </template>
            <template v-else>
              <div class="ce-toolbar"><span>实际请求 · 只读，不发送模型</span><button type="button" :disabled="!!pending" @click="preview">{{ pending === 'preview' ? '正在生成…' : '生成预览' }}</button></div>
              <details v-if="hints.length || serverIssues.length" class="ce-preview-validation" :open="!!serverErrors.length || hints.some(i => i.severity === 'error')"><summary>校验提示 · {{ hints.length + serverIssues.length }} 项 <small v-if="serverErrors.length">存在阻止创建的错误</small></summary><section aria-label="预览校验详情"><article v-for="(issue,i) in serverIssues" :key="`server-${i}`" class="ce-issue" :class="issue.severity"><span>{{ issue.text }}</span><small v-if="issue.path">{{ issue.path }}</small><button v-if="issueTarget(issue)" type="button" @click="inspectIssue(issue)">定位编辑 →</button></article><article v-for="(issue,i) in hints" :key="`local-${i}`" class="ce-issue" :class="issue.severity"><span>{{ issue.text }}</span><button v-if="issueTarget(issue)" type="button" @click="inspectIssue(issue)">定位编辑 →</button></article></section></details>
              <p v-if="automaticRepair" class="ce-help" role="status">{{ automaticRepair.text }}</p>
              <div v-if="previewReady && state.preview.payload != null" class="ce-editor-fill"><CodeEditor :model-value="payloadText" readonly label="实际请求预览 JSON" /></div>
              <div v-else class="ce-empty ce-preview-empty"><strong>{{ previewReady ? '未生成可用请求' : '尚未生成当前编辑稿的预览' }}</strong><p>{{ previewReady ? '请根据校验提示定位并修正内容。' : '生成服务端实际请求后，可在这里检查 JSON；不会调用模型。' }}</p></div>
              <small v-if="previewReady" class="ce-help">兼容续聊会自动处理受影响的历史思考和签名；预览与保存使用相同结果，不改原始编辑稿。</small>
            </template>
          </div>
        </main>
      </div>
      <footer class="ce-footer">
        <div class="ce-actions"><button type="button" :disabled="!state.undo.length" @click="history('undo')">撤销</button><button type="button" :disabled="!state.redo.length" @click="history('redo')">重做</button><small>{{ changes.length }} 项改动 · {{ state.savedEpoch === state.epoch ? '已保存 / 未改动' : '未保存' }}</small></div>
        <div class="ce-actions ce-submit"><button type="button" :disabled="!!pending" @click="saveDraft">{{ pending === 'save' ? '保存中…' : '保存草稿' }}</button><button type="button" :disabled="!!pending" @click="preview">校验并预览</button><input v-model="branchTitle" aria-label="副本标题" placeholder="副本标题"><button type="button" :disabled="!saveReady" @click="createBranch">{{ pending === 'create' ? '保存中…' : '保存副本' }}</button><button type="button" class="ce-primary ce-save-original" :disabled="!saveReady" @click="askSaveOriginal">{{ pending === 'apply' ? '保存中…' : '保存到原会话' }}</button></div>
      </footer>
    </template>
    </div>
    <div v-if="error || notice" class="ce-status" :class="{'ce-error': error}" :role="error ? 'alert' : 'status'"><span>{{ error || notice }}</span><button type="button" aria-label="关闭提示" @click="error = ''; notice = ''">×</button></div>
    <el-dialog :model-value="!!entityEditor" class="ce-dialog ce-edit-dialog ce-surface mobile-viewport-dialog" width="min(880px, calc(100vw - 32px))" top="16px" :title="entityEditor?.kind === 'tool' ? (entityEditor.index < 0 ? '添加工具定义' : `编辑工具 · ${entityEditor.data.name || '未命名'}`) : entityEditor?.kind === 'call' ? '编辑工具调用' : `编辑 ${entityEditor?.field || ''}`" append-to-body destroy-on-close :close-on-click-modal="false" @update:model-value="value => { if (!value) entityEditor = null; }">
      <template v-if="entityEditor">
        <nav v-if="entityEditor.kind !== 'field'" class="ce-tabs"><button type="button" :class="{active: entityEditor.view === 'fields'}" @click="entityView('fields')">字段编辑</button><button type="button" :class="{active: entityEditor.view === 'raw'}" @click="entityView('raw')">完整 JSON</button></nav>
        <div class="ce-dialog-content">
          <template v-if="entityEditor.view === 'fields' && entityEditor.kind !== 'field'">
            <div class="ce-message-meta"><label class="ce-grow">{{ entityEditor.kind === 'tool' ? '工具名称' : '调用工具' }}<input v-model="entityEditor.data.name" autofocus></label><label v-if="entityEditor.kind === 'call'" class="ce-grow">调用 ID<input v-model="entityEditor.data.id"></label></div>
            <label v-if="entityEditor.kind === 'tool'">说明<textarea v-model="entityEditor.data.description" rows="3" placeholder="工具用途及使用说明"></textarea></label>
            <div class="ce-toolbar"><strong>{{ entityEditor.kind === 'tool' ? '参数定义' : '调用参数' }}</strong><small>JSON · 支持查找和折叠</small></div>
          </template>
          <div class="ce-dialog-editor"><CodeEditor v-if="entityEditor.view === 'raw'" key="entity-raw" v-model="entityEditor.raw" label="完整定义 JSON" /><CodeEditor v-else key="entity-fields" v-model="entityEditor.text" :language="entityEditor.json ? 'json' : entityEditor.field === 'reasoning' ? 'markdown' : 'plaintext'" :label="entityEditor.kind === 'tool' ? '工具参数定义' : entityEditor.kind === 'call' ? '工具调用参数' : entityEditor.field" /></div>
          <p v-if="entityError" class="ce-error" role="alert">{{ entityError }}</p>
          <small v-if="entityEditor.kind === 'field' && (entityEditor.block || ['signature','reasoning','native_output_items'].includes(entityEditor.field))" class="ce-help">原有字段原样保留。手动修改不会重新签名，是否可续接仍需服务端校验。</small>
        </div>
      </template>
      <template #footer><div class="ce-actions"><small>保存前不会改动编辑稿</small><button type="button" @click="entityEditor = null">取消</button><button type="button" class="ce-primary" @click="saveEntity">保存修改</button></div></template>
    </el-dialog>
    <el-dialog :model-value="!!dialog" class="ce-dialog ce-delete-dialog ce-surface mobile-viewport-dialog" width="min(560px, calc(100vw - 32px))" top="16px" :title="dialog?.kind === 'refresh' ? '加载最新上下文' : dialog?.kind === 'apply' ? '保存到原会话' : '确认删除'" append-to-body destroy-on-close :close-on-click-modal="false" @update:model-value="value => { if (!value) dialog = null; }">
      <template v-if="dialog">
        <p v-if="dialog.kind === 'apply'">将当前 {{ doc.entries.length }} 条消息、系统提示词和工具定义保存到原会话，目标模型为 {{ state.targetModel }}。下一次消息会使用此上下文；原始聊天和工具执行记录不变，不会重新执行历史工具。保存后沿用冻结快照规则，不自动注入新的任务记忆。</p><p v-else-if="dialog.kind === 'refresh'">这会放弃当前页的编辑并载入源会话最新上下文。原会话和已保存的服务器草稿不会被删除；只有再次保存新稿时才会替换服务器草稿。</p><p v-else-if="dialog.kind === 'call'">按本批次 call ID 关联，保留其余并行调用。历史工具不会重新执行。</p><p v-else-if="dialog.kind === 'thinking'">删除整组思考，不伪造或重签。当前工具回合原样回放限制仍由服务器校验。</p><p v-else-if="dialog.kind === 'tool'">仅从编辑稿移除此工具定义，不卸载工具，也不删除历史调用。</p><p v-else>以下关联项将联动删除，不补造工具结果。可通过撤销恢复。</p>
        <label v-if="dialog.kind === 'call'"><input v-model="deleteMode" type="radio" value="paired"> 删除调用及关联返回</label><label v-if="dialog.kind === 'call'"><input v-model="deleteMode" type="radio" value="only"> 仅删调用，保留返回（自动转原始实验模式）</label><label v-if="dialog.kind === 'thinking'"><input v-model="thinkingScope" type="radio" value="suffix" @change="dialog.impact = reasoningImpact(doc,dialog.id,true)"> 本条及之后思考</label><label v-if="dialog.kind === 'thinking'"><input v-model="thinkingScope" type="radio" value="one" @change="dialog.impact = reasoningImpact(doc,dialog.id,false)"> 仅本条（后续签名需另行核对）</label>
        <div v-if="!['refresh', 'apply'].includes(dialog.kind)" class="ce-impact"><div v-for="path in dialog.kind === 'call' && deleteMode === 'only' ? dialog.impact.remove.filter(p => !dialog.impact.pair.results.some(id => p === `${id}.message`)) : dialog.kind === 'call' ? dialog.impact.remove : dialog.impact" :key="path">删除 · <code>{{ path }}</code></div><template v-if="dialog.kind === 'call'"><div v-for="id in dialog.impact.retain" :key="id">保留并行调用 · <code>{{ id }}</code></div><template v-if="deleteMode === 'only'"><div v-for="id in dialog.impact.pair.results" :key="id">保留孤儿结果 · <code>{{ id }}</code></div></template></template></div>
      </template>
      <template #footer><div class="ce-actions"><button type="button" @click="dialog = null">取消</button><button type="button" class="ce-primary" @click="confirmDelete">{{ dialog?.kind === 'refresh' ? '放弃本页编辑并加载' : dialog?.kind === 'apply' ? '保存到原会话' : '确认删除' }}</button></div></template>
    </el-dialog>
  </section>
</template>
