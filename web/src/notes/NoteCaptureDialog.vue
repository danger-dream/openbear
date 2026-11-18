<script setup>
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import { FilePlus2, ListTree, Search } from '@lucide/vue';
import { Api, apiError } from '../api.js';
import NoteIcon from './NoteIcon.vue';

const LAST_TARGET_KEY = 'openbear:kb-notes:last-capture-target';

const visible = ref(false);
const mode = ref('create'); // 'create' | 'append'
const title = ref('');
const parentUuid = ref('');
const targetNoteUuid = ref('');
const noteText = ref('');
const quoteText = ref('');
const sourceMeta = ref({
  conversationUuid: '',
  conversationTitle: '',
  opId: '',
  turnUuid: '',
  turnNumber: 0,
});

const treeItems = ref([]);
const loadingTree = ref(false);
const filterQuery = ref('');
const submitting = ref(false);

const filteredNotes = computed(() => {
  const q = filterQuery.value.trim().toLowerCase();
  if (!q) return treeItems.value;
  return treeItems.value.filter((item) =>
    `${item.title || ''} ${item.path || ''} ${item.summary || ''}`.toLowerCase().includes(q),
  );
});

const hasSourceLink = computed(() => {
  const cid = String(sourceMeta.value.conversationUuid || '').trim();
  return Boolean(cid && !cid.startsWith('local:'));
});

function deriveDefaultTitle(contentStr, quoteStr, convTitle) {
  const raw = String(contentStr || quoteStr || convTitle || '会话摘录')
    .replace(/^[#>*\-\s`]+/gm, '')
    .trim();
  const firstLine = (raw.split('\n').find((line) => line.trim()) || '会话摘录').trim();
  return firstLine.length > 42 ? `${firstLine.slice(0, 42)}…` : firstLine;
}

async function loadNotesList() {
  loadingTree.value = true;
  try {
    const res = await Api.kbNotesTree();
    treeItems.value = Array.isArray(res?.items) ? res.items : [];
    let lastTarget = '';
    try {
      lastTarget = String(localStorage.getItem(LAST_TARGET_KEY) || '');
    } catch { /* ignore */ }
    if (lastTarget && treeItems.value.some((n) => n.noteUuid === lastTarget)) {
      targetNoteUuid.value = lastTarget;
    } else if (!targetNoteUuid.value && treeItems.value.length) {
      targetNoteUuid.value = treeItems.value[0].noteUuid;
    }
  } catch {
    treeItems.value = [];
  } finally {
    loadingTree.value = false;
  }
}

function openFromEvent(event) {
  const detail = event?.detail || {};
  quoteText.value = String(detail.quote || '').trim();
  noteText.value = String(detail.content || '').trim();
  sourceMeta.value = {
    conversationUuid: String(detail.conversationUuid || ''),
    conversationTitle: String(detail.conversationTitle || ''),
    opId: String(detail.opId || ''),
    turnUuid: String(detail.turnUuid || ''),
    turnNumber: Number(detail.turnNumber || 0),
  };
  title.value = deriveDefaultTitle(noteText.value, quoteText.value, sourceMeta.value.conversationTitle);
  filterQuery.value = '';
  mode.value = detail.defaultMode === 'append' ? 'append' : 'create';
  visible.value = true;
  void loadNotesList();
}

watch(mode, (nextMode) => {
  if (nextMode === 'append' && !targetNoteUuid.value && treeItems.value.length) {
    targetNoteUuid.value = treeItems.value[0].noteUuid;
  }
});

async function submit(openAfter = false) {
  if (submitting.value) return;
  if (!quoteText.value.trim() && !noteText.value.trim()) {
    ElMessage.warning('摘录或笔记内容不能为空');
    return;
  }
  if (mode.value === 'append' && !targetNoteUuid.value) {
    ElMessage.warning('请选择要追加的目标笔记');
    return;
  }
  submitting.value = true;
  try {
    const res = await Api.captureKbNote({
      mode: mode.value,
      title: title.value.trim() || '会话摘录',
      parentUuid: mode.value === 'create' ? parentUuid.value : '',
      targetNoteUuid: mode.value === 'append' ? targetNoteUuid.value : '',
      quote: quoteText.value,
      content: noteText.value,
      conversationUuid: sourceMeta.value.conversationUuid,
      conversationTitle: sourceMeta.value.conversationTitle,
      opId: sourceMeta.value.opId,
      turnUuid: sourceMeta.value.turnUuid,
      turnNumber: sourceMeta.value.turnNumber,
    });
    const savedUuid = res?.note?.noteUuid || '';
    if (mode.value === 'append' && savedUuid) {
      try {
        localStorage.setItem(LAST_TARGET_KEY, savedUuid);
      } catch { /* ignore */ }
    }
    visible.value = false;
    ElMessage.success(mode.value === 'append' ? '已追加到笔记' : '已存入笔记');
    if (openAfter && savedUuid) {
      window.dispatchEvent(new CustomEvent('openbear:open-kb-note', { detail: { noteUuid: savedUuid } }));
    }
  } catch (err) {
    ElMessage.error(apiError(err));
  } finally {
    submitting.value = false;
  }
}

onMounted(() => {
  window.addEventListener('openbear:capture-to-kb-note', openFromEvent);
});

onBeforeUnmount(() => {
  window.removeEventListener('openbear:capture-to-kb-note', openFromEvent);
});
</script>

<template>
  <el-dialog
    v-model="visible"
    title="存入全局笔记"
    width="500px"
    append-to-body
    class="kb-capture-dialog mobile-viewport-dialog"
  >
    <div class="kb-capture-body">
      <div class="kb-capture-seg" role="tablist" aria-label="保存方式">
        <button
          type="button"
          role="tab"
          :aria-selected="mode === 'create'"
          :class="{ active: mode === 'create' }"
          @click="mode = 'create'"
        >
          <FilePlus2 :size="14" aria-hidden="true" />
          <span>新建笔记</span>
        </button>
        <button
          type="button"
          role="tab"
          :aria-selected="mode === 'append'"
          :class="{ active: mode === 'append' }"
          @click="mode = 'append'"
        >
          <ListTree :size="14" aria-hidden="true" />
          <span>追加到已有笔记</span>
        </button>
      </div>

      <template v-if="mode === 'create'">
        <label class="kb-capture-field">
          <span>笔记标题</span>
          <input v-model="title" type="text" maxlength="200" placeholder="输入笔记标题…" />
        </label>
        <label class="kb-capture-field">
          <span>存放位置（可选父笔记）</span>
          <select v-model="parentUuid" aria-label="存放父笔记">
            <option value="">顶层笔记（根目录）</option>
            <option v-for="item in treeItems" :key="item.noteUuid" :value="item.noteUuid">
              {{ item.icon ? `${item.icon} ` : '' }}{{ item.path || item.title }}
            </option>
          </select>
        </label>
      </template>

      <template v-else>
        <div class="kb-capture-field">
          <span>选择要追加的目标笔记</span>
          <div class="kb-capture-search">
            <Search :size="13.5" aria-hidden="true" />
            <input v-model="filterQuery" type="search" placeholder="搜索笔记标题或路径…" />
          </div>
          <div class="kb-capture-target-list" role="listbox" aria-label="目标笔记列表">
            <div v-if="loadingTree" class="kb-capture-empty">加载笔记目录中…</div>
            <div v-else-if="!filteredNotes.length" class="kb-capture-empty">
              {{ treeItems.length ? '没有匹配的笔记' : '当前还没有已有笔记，请切到「新建笔记」' }}
            </div>
            <button
              v-for="item in filteredNotes"
              :key="item.noteUuid"
              type="button"
              role="option"
              class="kb-capture-target-row"
              :aria-selected="targetNoteUuid === item.noteUuid"
              :class="{ active: targetNoteUuid === item.noteUuid }"
              @click="targetNoteUuid = item.noteUuid"
            >
              <NoteIcon :icon="item.icon" :has-children="Boolean(item.childCount)" :size="15" />
              <span class="kb-capture-target-main">
                <strong>{{ item.title || '未命名笔记' }}</strong>
                <small v-if="item.path && item.path !== item.title">{{ item.path }}</small>
              </span>
            </button>
          </div>
        </div>
      </template>

      <div v-if="quoteText" class="kb-capture-quote-box">
        <div class="kb-capture-quote-head">
          <span>原文摘录</span>
          <small v-if="hasSourceLink">
            自动附带{{ sourceMeta.turnNumber ? `第 ${sourceMeta.turnNumber} 轮` : '原会话' }}溯源胶囊
          </small>
        </div>
        <blockquote>{{ quoteText }}</blockquote>
      </div>

      <label class="kb-capture-field">
        <span>补充批注 / 正文（支持 Markdown）</span>
        <textarea
          v-model="noteText"
          rows="3"
          maxlength="32000"
          placeholder="可留空直接保存摘录，或补充你的整理说明…"
        ></textarea>
      </label>
    </div>

    <template #footer>
      <div class="kb-capture-foot">
        <button type="button" class="kb-capture-btn" @click="visible = false">取消</button>
        <button
          type="button"
          class="kb-capture-btn"
          :disabled="submitting"
          @click="submit(true)"
        >
          保存并打开笔记
        </button>
        <button
          type="button"
          class="kb-capture-btn is-primary"
          :disabled="submitting"
          @click="submit(false)"
        >
          {{ submitting ? '保存中…' : mode === 'append' ? '确认追加' : '保存笔记' }}
        </button>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
.kb-capture-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.kb-capture-seg {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px;
  padding: 3px;
  border-radius: 9px;
  background: var(--ob-surface-muted);
  border: 1px solid var(--ob-border);
}
.kb-capture-seg button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 32px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--ob-text-subtle);
  font-size: 12.5px;
  font-weight: 500;
  cursor: pointer;
}
.kb-capture-seg button.active {
  background: var(--ob-surface);
  color: var(--ob-text);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.06);
}
.kb-capture-field {
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 12px;
  color: var(--ob-text-subtle);
}
.kb-capture-field input,
.kb-capture-field select,
.kb-capture-field textarea {
  width: 100%;
  border: 1px solid var(--ob-border-strong);
  border-radius: 8px;
  background: var(--ob-surface);
  color: var(--ob-text);
  padding: 7px 10px;
  font-family: inherit;
  font-size: 13px;
  line-height: 1.5;
}
.kb-capture-search {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 0 9px;
  border: 1px solid var(--ob-border-strong);
  border-radius: 8px;
  background: var(--ob-surface);
}
.kb-capture-search input {
  border: 0;
  padding: 6px 0;
  outline: none;
  background: transparent;
}
.kb-capture-target-list {
  max-height: 168px;
  overflow-y: auto;
  border: 1px solid var(--ob-border);
  border-radius: 8px;
  padding: 4px;
  background: var(--ob-surface-muted);
}
.kb-capture-target-row {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 6px 8px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--ob-text);
  text-align: left;
  cursor: pointer;
}
.kb-capture-target-row:hover {
  background: var(--ob-fill-hover, rgba(127, 127, 127, 0.08));
}
.kb-capture-target-row.active {
  background: var(--ob-chat-selected, rgba(59, 130, 246, 0.12));
}
.kb-capture-target-icon {
  flex: none;
  font-size: 14px;
}
.kb-capture-target-main {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.kb-capture-target-main strong {
  font-size: 12.5px;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kb-capture-target-main small {
  font-size: 11px;
  color: var(--ob-text-subtle);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.kb-capture-empty {
  padding: 16px 8px;
  text-align: center;
  color: var(--ob-text-subtle);
  font-size: 12px;
}
.kb-capture-quote-box {
  border: 1px solid var(--ob-border);
  border-radius: 8px;
  padding: 8px 10px;
  background: var(--ob-surface-muted);
}
.kb-capture-quote-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
  font-size: 11.5px;
  color: var(--ob-text-subtle);
}
.kb-capture-quote-box blockquote {
  margin: 0;
  padding-left: 8px;
  border-left: 2.5px solid var(--ob-border-strong);
  max-height: 92px;
  overflow-y: auto;
  font-size: 12px;
  line-height: 1.55;
  color: var(--ob-text);
  white-space: pre-wrap;
}
.kb-capture-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}
.kb-capture-btn {
  height: 32px;
  padding: 0 12px;
  border-radius: 7px;
  border: 1px solid var(--ob-border-strong);
  background: var(--ob-surface);
  color: var(--ob-text);
  font-size: 12.5px;
  cursor: pointer;
}
.kb-capture-btn.is-primary {
  background: var(--el-color-primary);
  border-color: var(--el-color-primary);
  color: #fff;
}
.kb-capture-btn:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
@media (max-width: 760px) {
  .kb-capture-field input,
  .kb-capture-field select,
  .kb-capture-field textarea {
    font-size: 16px;
  }
}
</style>
