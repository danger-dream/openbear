import {computed, inject, ref, watch} from 'vue';

export const MESSAGE_VISIBILITY = Symbol('message-visibility');
export const visibilityOperationId = value => String(value?.operation?.opId || value?.opId || value?.eventKey || value?.id || '');
const types = new Set(['user_message', 'assistant_message', 'reasoning', 'tool', 'agent', 'user_interaction', 'context_compaction', 'model_retry', 'notice']);
const empty = () => ({revision: -1, hiddenIds: [], items: []});

export function createMessageVisibility({conversationUuid, operations, api, beforeChange = () => {}, afterChange = () => {}, preserveSelectionPosition = () => false, onError = () => {}}) {
  const snapshot = ref(empty());
  const busy = ref(false), managing = ref(false), selecting = ref(false), selected = ref(new Set()), undoIds = ref([]);
  const mobileMenu = ref(null);
  const hiddenIds = computed(() => new Set(snapshot.value.hiddenIds));
  const items = computed(() => snapshot.value.items);
  let generation = 0;
  watch(conversationUuid, () => {
    generation++;
    snapshot.value = empty(); busy.value = false; managing.value = false;
    selecting.value = false; selected.value = new Set(); undoIds.value = []; mobileMenu.value = null;
  }, {flush: 'sync'});
  const canTarget = value => {
    const op = operations.value.get(visibilityOperationId(value));
    return Boolean(op && !op.internal && !op.payload?.internal && !op.payload?.hidden && types.has(op.opType));
  };
  const isHidden = value => hiddenIds.value.has(visibilityOperationId(value));
  function apply(value, preservePosition = true) {
    if (!value || value.conversationUuid !== conversationUuid.value || Number(value.revision) < snapshot.value.revision) return false;
    if (Number(value.revision) === snapshot.value.revision) return false;
    const anchor = preservePosition ? beforeChange() : null;
    snapshot.value = {revision: Number(value.revision) || 0, hiddenIds: value.hiddenIds || [], items: value.items || []};
    selected.value = new Set([...selected.value].filter(id => !hiddenIds.value.has(id)));
    undoIds.value = undoIds.value.filter(id => hiddenIds.value.has(id));
    if (preservePosition) afterChange(anchor);
    return true;
  }
  async function mutate(data) {
    if (busy.value || !conversationUuid.value || conversationUuid.value.startsWith('local:')) return false;
    const uuid = conversationUuid.value, turn = generation;
    busy.value = true;
    try {
      const result = await api.updateMessageVisibility(uuid, data);
      if (turn !== generation || uuid !== conversationUuid.value) return false;
      apply(result.visibility);
      if (data.hidden) {
        undoIds.value = data.opIds.filter(id => hiddenIds.value.has(id));
        selecting.value = false; selected.value = new Set();
      }
      return true;
    } catch (error) {
      if (turn === generation) onError(error);
      return false;
    } finally { if (turn === generation) busy.value = false; }
  }
  function openMobileMenu(target, turn = null) {
    if (busy.value || !canTarget(target) || isHidden(target)) return;
    mobileMenu.value = {target, turn, preview: visibilityTargetPreview(target, operations.value.get(visibilityOperationId(target)))};
  }
  function setSelectionMode(active) {
    if (selecting.value === active) return;
    const preserve = preserveSelectionPosition();
    const anchor = preserve ? beforeChange() : null;
    selecting.value = active;
    if (preserve) afterChange(anchor);
  }
  function startSelection(value) {
    mobileMenu.value = null;
    setSelectionMode(true);
    selected.value = new Set(canTarget(value) ? [visibilityOperationId(value)] : []);
  }
  function toggle(value) {
    if (!canTarget(value)) return;
    const id = visibilityOperationId(value), next = new Set(selected.value);
    next.has(id) ? next.delete(id) : next.add(id);
    selected.value = next;
  }
  function cancelSelection() { setSelectionMode(false); selected.value = new Set(); }
  const hide = values => mutate({opIds: [...new Set(values.map(visibilityOperationId))], hidden: true});
  // Use the projected round, not the whole conversation; interruption rows are
  // user_message operations even though they live inside turn.events.
  const assistantTargets = turn => (Array.isArray(turn?.events) ? turn.events : []).filter(value => {
    const op = operations.value.get(visibilityOperationId(value));
    return canTarget(value) && op.opType !== 'user_message' && !isHidden(value);
  });
  function hideAssistantTurn(turn) {
    const targets = assistantTargets(turn);
    return targets.length ? hide(targets) : Promise.resolve(false);
  }
  const restore = ids => mutate({opIds: ids, hidden: false});
  return {snapshot, hiddenIds, items, busy, managing, selecting, selected, undoIds, mobileMenu, openMobileMenu,
    canTarget, isHidden, apply, mutate, startSelection, toggle, cancelSelection, hide, restore, assistantTargets, hideAssistantTurn,
    restoreAll: () => mutate({restoreAll: true}),
    hideSelected: () => hide([...selected.value].map(id => ({opId: id}))),
    undo: () => restore(undoIds.value),
    userContent: turn => isHidden(turn?.user) ? '' : String(turn?.user?.content || ''),
  };
}

export function visibilityTargetPreview(target, operation = {}) {
  const labels = {user_message: '你的消息', assistant_message: '模型回复', reasoning: '思考过程', tool: '工具记录', agent: 'Agent 记录', user_interaction: '交互记录', context_compaction: '上下文整理', model_retry: '模型重试', notice: '状态记录'};
  const label = labels[operation.opType] || '消息';
  const payload = operation.payload || {};
  const text = [target?.message?.content, target?.content, target?.preview, payload.text, payload.content, payload.preview]
    .find(value => typeof value === 'string' && value.trim());
  const files = target?.attachments || payload.attachments || [];
  const fallbackText = Array.isArray(files) && files.length ? `附件：${files.map(file => file.fileName || file.name || '文件').join('、')}`
    : target?.retry?.attempt ? `第 ${target.retry.attempt} 次重试` : target?.toolName || '无文字内容';
  const compact = String(text || fallbackText).replace(/\s+/g, ' ').trim();
  return {label, text: compact.length > 140 ? `${compact.slice(0, 139)}…` : compact};
}

export function visibilitySelectionClasses(value, visibility) {
  const selectable = Boolean(visibility.selecting.value && visibility.canTarget(value));
  const menuTarget = visibility.mobileMenu?.value?.target;
  const menuActive = menuTarget && visibilityOperationId(menuTarget) === visibilityOperationId(value);
  return {'visibility-selectable': selectable, 'visibility-selected': selectable && visibility.selected.value.has(visibilityOperationId(value)),
    ...(menuActive ? {'visibility-menu-target': true} : {})};
}

export function selectVisibilityRow(event, value, visibility, viewport = globalThis.window) {
  if (!visibility.selecting.value || !visibility.canTarget(value)) return;
  // Native checkbox/menu handlers own their clicks; selecting text remains possible.
  if (event.target?.closest?.('.message-visibility-action') || viewport?.getSelection?.()?.toString()) return;
  event.preventDefault();
  event.stopPropagation();
  if (!visibility.busy.value) visibility.toggle(value);
}

const fallback = {
  canTarget: () => false, isHidden: () => false, assistantTargets: () => [], userContent: turn => String(turn?.user?.content || ''),
  hiddenIds: ref(new Set()), selecting: ref(false), selected: ref(new Set()), busy: ref(false),
};
export const useMessageVisibility = () => inject(MESSAGE_VISIBILITY, fallback);
