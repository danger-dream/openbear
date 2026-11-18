import { shallowReactive } from 'vue';
import {
  Extension,
  InputRule,
  Mark,
  Node,
  markInputRule,
  mergeAttributes,
  textblockTypeInputRule,
  wrappingInputRule,
} from '@tiptap/core';
import { VueNodeViewRenderer } from '@tiptap/vue-3';
import { history, undo, redo, undoDepth, redoDepth } from '@tiptap/pm/history';
import { Plugin, PluginKey, TextSelection } from '@tiptap/pm/state';
import { Decoration, DecorationSet } from '@tiptap/pm/view';
import { dropCursor } from '@tiptap/pm/dropcursor';
import { gapCursor } from '@tiptap/pm/gapcursor';
import {
  chainCommands,
  exitCode,
  liftEmptyBlock,
  newlineInCode,
  setBlockType,
  splitBlock,
  toggleMark,
  wrapIn,
} from '@tiptap/pm/commands';
import {
  liftListItem,
  sinkListItem,
  splitListItem,
  wrapInList,
} from '@tiptap/pm/schema-list';
import {
  TableMap,
  addColumnAfter,
  addColumnBefore,
  addRowAfter,
  addRowBefore,
  columnResizing,
  deleteColumn,
  deleteRow,
  deleteTable,
  fixTables,
  goToNextCell,
  isInTable,
  selectedRect,
  setCellAttr,
  tableEditing,
  toggleHeaderRow,
} from '@tiptap/pm/tables';
import hljs from 'highlight.js';
import ReferenceNodeView from '../references/ReferenceNodeView.vue';
import { referenceFromUrl, referenceUrl } from '../references/codec.js';
import NoteCodeBlockView from './NoteCodeBlockView.vue';
import NoteImageBlockView from './NoteImageBlockView.vue';
import NoteTaskItemView from './NoteTaskItemView.vue';
import { docToMarkdown, parseHljsHtmlToRanges, safeLinkHref } from './markdownCodec.js';

export { parseHljsHtmlToRanges };

export const blockSelectionPluginKey = new PluginKey('syBlockSelection');

export function resolveBlockTargetAtPos(doc, rawPos) {
  if (!doc || typeof rawPos !== 'number') return null;
  const clamped = Math.max(0, Math.min(rawPos, doc.content.size));
  const $pos = doc.resolve(clamped);

  // Check if clamped sits directly at a block node boundary (e.g. atom horizontalRule / imageBlock / table / codeBlock)
  const directAfter = $pos.nodeAfter;
  if (
    directAfter
    && ['table', 'codeBlock', 'imageBlock', 'horizontalRule', 'bulletList', 'orderedList', 'taskList', 'blockquote', 'heading', 'paragraph'].includes(directAfter.type.name)
    && $pos.depth === 0
  ) {
    return {
      pos: clamped,
      endPos: clamped + directAfter.nodeSize,
      node: directAfter,
      kind: directAfter.type.name,
      level: Number(directAfter.attrs?.level || 1),
      parentContainer: null,
    };
  }

  if ($pos.depth === 0) return null;

  // 1. If inside a table or codeBlock or imageBlock, always target that atomic/structured block
  for (let d = $pos.depth; d >= 1; d -= 1) {
    const n = $pos.node(d);
    if (n.type.name === 'table' || n.type.name === 'codeBlock' || n.type.name === 'imageBlock' || n.type.name === 'horizontalRule') {
      let parentContainer = null;
      for (let pd = d - 1; pd >= 1; pd -= 1) {
        const pn = $pos.node(pd);
        if (['bulletList', 'orderedList', 'taskList', 'blockquote'].includes(pn.type.name)) {
          parentContainer = {
            pos: $pos.before(pd),
            endPos: $pos.after(pd),
            node: pn,
            kind: pn.type.name,
          };
          break;
        }
      }
      return {
        pos: $pos.before(d),
        endPos: $pos.after(d),
        node: n,
        kind: n.type.name,
        level: 1,
        parentContainer,
      };
    }
  }

  // 2. If inside a listItem or taskItem
  for (let d = $pos.depth; d >= 1; d -= 1) {
    const n = $pos.node(d);
    if (n.type.name === 'listItem' || n.type.name === 'taskItem') {
      const listDepth = d - 1;
      const listNode = listDepth >= 1 ? $pos.node(listDepth) : null;
      if (listNode && listNode.childCount === 1) {
        return {
          pos: $pos.before(listDepth),
          endPos: $pos.after(listDepth),
          node: listNode,
          kind: listNode.type.name,
          level: 1,
          parentContainer: null,
        };
      }
      return {
        pos: $pos.before(d),
        endPos: $pos.after(d),
        node: n,
        kind: listNode?.type.name === 'orderedList' ? 'orderedList' : n.type.name,
        level: 1,
        parentContainer: listNode
          ? {
              pos: $pos.before(listDepth),
              endPos: $pos.after(listDepth),
              node: listNode,
              kind: listNode.type.name,
            }
          : null,
      };
    }
  }

  // 3. If inside a blockquote
  for (let d = $pos.depth; d >= 1; d -= 1) {
    const n = $pos.node(d);
    if (n.type.name === 'blockquote') {
      if (n.childCount === 1 || $pos.depth === d) {
        return {
          pos: $pos.before(d),
          endPos: $pos.after(d),
          node: n,
          kind: 'blockquote',
          level: 1,
          parentContainer: null,
        };
      }
      const childDepth = d + 1;
      const childNode = $pos.node(childDepth);
      return {
        pos: $pos.before(childDepth),
        endPos: $pos.after(childDepth),
        node: childNode,
        kind: childNode.type.name,
        level: Number(childNode.attrs?.level || 1),
        parentContainer: {
          pos: $pos.before(d),
          endPos: $pos.after(d),
          node: n,
          kind: 'blockquote',
        },
      };
    }
  }

  // 4. Default top-level block (depth 1: paragraph, heading, etc.)
  const topNode = $pos.node(1);
  return {
    pos: $pos.before(1),
    endPos: $pos.after(1),
    node: topNode,
    kind: topNode.type.name,
    level: Number(topNode.attrs?.level || 1),
    parentContainer: null,
  };
}

export function serializeSingleBlockMarkdown(node) {
  if (!node) return '';
  return docToMarkdown({ type: 'doc', content: [node.toJSON()] });
}

function expandSingleChildDeleteRange(doc, from, to) {
  let curFrom = from;
  let curTo = to;
  while (curFrom > 0) {
    const $from = doc.resolve(curFrom);
    if ($from.depth < 1) break;
    const parent = $from.parent;
    if (
      ['bulletList', 'orderedList', 'taskList', 'blockquote'].includes(parent.type.name)
      && parent.childCount === 1
    ) {
      curFrom = $from.before($from.depth);
      curTo = $from.after($from.depth);
    } else {
      break;
    }
  }
  return { from: curFrom, to: curTo };
}

export function deleteBlockAtPos(state, dispatch, blockPos) {
  const node = state.doc.nodeAt(blockPos);
  if (!node) return false;
  const { from, to } = expandSingleChildDeleteRange(state.doc, blockPos, blockPos + node.nodeSize);
  if (!dispatch) return true;

  const tr = state.tr.setMeta(blockSelectionPluginKey, { clear: true });
  if (from === 0 && to >= state.doc.content.size) {
    const emptyPara = state.schema.nodes.paragraph.create();
    tr.replaceWith(0, state.doc.content.size, emptyPara);
    tr.setSelection(TextSelection.near(tr.doc.resolve(1)));
  } else {
    tr.delete(from, to);
    const safePos = Math.max(0, Math.min(from, tr.doc.content.size));
    tr.setSelection(TextSelection.near(tr.doc.resolve(safePos)));
  }
  dispatch(tr.scrollIntoView());
  return true;
}

export function duplicateBlockAtPos(state, dispatch, blockPos) {
  const node = state.doc.nodeAt(blockPos);
  if (!node) return false;
  if (!dispatch) return true;
  const clone = node.type.create(node.attrs, node.content, node.marks);
  const insertPos = blockPos + node.nodeSize;
  const tr = state.tr.insert(insertPos, clone);
  tr.setMeta(blockSelectionPluginKey, { pos: insertPos });
  dispatch(tr.scrollIntoView());
  return true;
}

export function moveBlockByOffset(state, dispatch, blockPos, direction) {
  const node = state.doc.nodeAt(blockPos);
  if (!node) return false;
  const $pos = state.doc.resolve(blockPos);
  const parent = $pos.parent;
  const index = $pos.index($pos.depth);
  const targetIndex = index + (direction < 0 ? -1 : 1);
  if (targetIndex < 0 || targetIndex >= parent.childCount) return false;
  if (!dispatch) return true;

  const sibling = parent.child(targetIndex);
  let tr = state.tr;
  if (direction < 0) {
    const prevPos = blockPos - sibling.nodeSize;
    tr = tr.delete(blockPos, blockPos + node.nodeSize);
    tr = tr.insert(prevPos, node);
    tr.setMeta(blockSelectionPluginKey, { pos: prevPos });
  } else {
    const afterNext = blockPos + node.nodeSize + sibling.nodeSize;
    tr = tr.insert(afterNext, node);
    tr = tr.delete(blockPos, blockPos + node.nodeSize);
    tr.setMeta(blockSelectionPluginKey, { pos: blockPos + sibling.nodeSize });
  }
  dispatch(tr.scrollIntoView());
  return true;
}

export function moveBlockToTopLevelPos(state, dispatch, fromPos, targetPos) {
  const node = state.doc.nodeAt(fromPos);
  if (!node) return false;
  const { from, to } = expandSingleChildDeleteRange(state.doc, fromPos, fromPos + node.nodeSize);
  const movingNode = state.doc.nodeAt(from);
  if (!movingNode) return false;
  if (targetPos >= from && targetPos <= to) return false;
  if (!dispatch) return true;

  let tr = state.tr;
  let finalPos = targetPos;
  if (targetPos > from) {
    tr = tr.insert(targetPos, movingNode);
    tr = tr.delete(from, to);
    finalPos = targetPos - (to - from);
  } else {
    tr = tr.delete(from, to);
    tr = tr.insert(targetPos, movingNode);
    finalPos = targetPos;
  }
  tr.setMeta(blockSelectionPluginKey, { pos: finalPos });
  dispatch(tr.scrollIntoView());
  return true;
}

export function insertParagraphAroundBlock(state, dispatch, blockPos, position = 'after') {
  const node = state.doc.nodeAt(blockPos);
  if (!node) return false;
  // Find top-level block boundary so inserting around a listItem or table always creates a clean paragraph
  const $pos = state.doc.resolve(blockPos);
  const topFrom = $pos.depth >= 1 ? $pos.before(1) : blockPos;
  const topNode = state.doc.nodeAt(topFrom) || node;
  const insertAt = position === 'before' ? topFrom : topFrom + topNode.nodeSize;
  if (!dispatch) return true;

  const para = state.schema.nodes.paragraph.create();
  const tr = state.tr.insert(insertAt, para);
  tr.setMeta(blockSelectionPluginKey, { clear: true });
  tr.setSelection(TextSelection.create(tr.doc, insertAt + 1));
  dispatch(tr.scrollIntoView());
  return true;
}

export function canUndoRedo(state) {
  if (!state) return { undo: false, redo: false };
  return {
    undo: undoDepth(state) > 0,
    redo: redoDepth(state) > 0,
  };
}

export function findInlineMemoRangeAtPos(doc, rawPos) {
  const memoType = doc?.type?.schema?.marks?.inlineMemo;
  if (!memoType || typeof rawPos !== 'number') return null;
  const clamped = Math.max(0, Math.min(rawPos, doc.content.size));
  const $pos = doc.resolve(clamped);
  const parent = $pos.parent;
  if (!parent.isTextblock) return null;

  const parentStart = $pos.start($pos.depth);
  let targetRange = null;
  let offset = 0;
  parent.forEach((child) => {
    const childFrom = parentStart + offset;
    const childTo = childFrom + child.nodeSize;
    const mark = child.marks?.find?.((m) => m.type === memoType);
    if (mark && clamped >= childFrom && clamped <= childTo) {
      targetRange = {
        from: childFrom,
        to: childTo,
        text: child.text || '',
        memo: String(mark.attrs?.memo || ''),
      };
    }
    offset += child.nodeSize;
  });
  return targetRange;
}

function buildCodeHighlightDecorations(doc) {
  const decorations = [];
  doc.descendants((node, pos) => {
    if (node.type.name !== 'codeBlock') return true;
    const code = node.textContent || '';
    if (!code || code.length > 80000) return false;
    const rawLang = String(node.attrs?.language || '').toLowerCase();
    const lang = rawLang && hljs.getLanguage(rawLang) ? rawLang : '';
    if (!lang) return false;
    try {
      const res = hljs.highlight(code, { language: lang, ignoreIllegals: true });
      const ranges = parseHljsHtmlToRanges(res.value, code.length);
      for (const r of ranges) {
        decorations.push(
          Decoration.inline(pos + 1 + r.from, pos + 1 + r.to, { class: r.className }),
        );
      }
    } catch {
      // Skip highlight on unknown grammar edge cases
    }
    return false;
  });
  return DecorationSet.create(doc, decorations);
}

const DocumentNode = Node.create({
  name: 'doc',
  topNode: true,
  content: 'block+',
});

const TextNode = Node.create({
  name: 'text',
  group: 'inline',
});

const ParagraphNode = Node.create({
  name: 'paragraph',
  group: 'block',
  content: 'inline*',
  parseHTML() {
    return [{ tag: 'p' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['p', mergeAttributes(HTMLAttributes), 0];
  },
  addCommands() {
    return {
      setParagraph: () => ({ state, dispatch }) => setBlockType(this.type)(state, dispatch),
    };
  },
});

const HeadingNode = Node.create({
  name: 'heading',
  group: 'block',
  content: 'inline*',
  defining: true,
  addAttributes() {
    return {
      level: { default: 1 },
    };
  },
  parseHTML() {
    return [1, 2, 3, 4, 5, 6].map((level) => ({
      tag: `h${level}`,
      attrs: { level },
    }));
  },
  renderHTML({ node, HTMLAttributes }) {
    const level = Math.max(1, Math.min(6, Number(node.attrs.level || 1)));
    return [`h${level}`, mergeAttributes(HTMLAttributes, { 'data-heading-level': level }), 0];
  },
  addCommands() {
    return {
      setHeading: ({ level }) => ({ state, dispatch }) => setBlockType(this.type, { level })(state, dispatch),
      toggleHeading: ({ level }) => ({ state, dispatch }) => {
        const { $from } = state.selection;
        if ($from.parent.type === this.type && $from.parent.attrs.level === level) {
          return setBlockType(state.schema.nodes.paragraph)(state, dispatch);
        }
        return setBlockType(this.type, { level })(state, dispatch);
      },
    };
  },
  addInputRules() {
    return [1, 2, 3, 4, 5, 6].map((level) =>
      textblockTypeInputRule({
        find: new RegExp(`^(#{${level}})\\s$`),
        type: this.type,
        getAttributes: { level },
      }),
    );
  },
  addKeyboardShortcuts() {
    const map = {};
    for (let level = 1; level <= 6; level += 1) {
      map[`Mod-Alt-${level}`] = () => this.editor.commands.toggleHeading({ level });
    }
    return map;
  },
});

const HardBreakNode = Node.create({
  name: 'hardBreak',
  group: 'inline',
  inline: true,
  selectable: false,
  addAttributes() {
    // A source line break (soft) is written back as a plain newline.
    return { soft: { default: false, rendered: false, keepOnSplit: false } };
  },
  parseHTML() {
    return [{ tag: 'br' }];
  },
  renderHTML() {
    return ['br'];
  },
  addKeyboardShortcuts() {
    return {
      'Shift-Enter': () => {
        this.editor.commands.insertContent({ type: 'hardBreak' });
        return true;
      },
    };
  },
});

const HorizontalRuleNode = Node.create({
  name: 'horizontalRule',
  group: 'block',
  parseHTML() {
    return [{ tag: 'hr' }];
  },
  renderHTML() {
    return ['hr'];
  },
  addCommands() {
    return {
      setHorizontalRule: () => ({ chain }) =>
        chain()
          .insertContent([{ type: this.name }, { type: 'paragraph' }])
          .run(),
    };
  },
  addInputRules() {
    return [
      new InputRule({
        find: /^(?:---|___|\*\*\*)\s$/,
        handler: ({ state, range }) => {
          const { tr, schema } = state;
          tr.replaceWith(range.from - 1, range.to, [
            schema.nodes.horizontalRule.create(),
            schema.nodes.paragraph.create(),
          ]);
        },
      }),
    ];
  },
});

const BlockquoteNode = Node.create({
  name: 'blockquote',
  group: 'block',
  content: 'block+',
  defining: true,
  parseHTML() {
    return [{ tag: 'blockquote' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['blockquote', mergeAttributes(HTMLAttributes), 0];
  },
  addCommands() {
    return {
      toggleBlockquote: () => ({ state, dispatch }) => {
        const { $from } = state.selection;
        for (let d = $from.depth; d > 0; d -= 1) {
          if ($from.node(d).type === this.type) {
            return liftEmptyBlock(state, dispatch) || false;
          }
        }
        return wrapIn(this.type)(state, dispatch);
      },
    };
  },
  addInputRules() {
    return [
      wrappingInputRule({
        find: /^\s*>\s$/,
        type: this.type,
      }),
    ];
  },
});

const BulletListNode = Node.create({
  name: 'bulletList',
  group: 'block',
  content: 'listItem+',
  parseHTML() {
    return [{ tag: 'ul:not([data-type="taskList"])' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['ul', mergeAttributes(HTMLAttributes), 0];
  },
  addCommands() {
    return {
      toggleBulletList: () => ({ state, dispatch }) => {
        const { $from } = state.selection;
        for (let d = $from.depth; d > 0; d -= 1) {
          if ($from.node(d).type === this.type) {
            return liftListItem(state.schema.nodes.listItem)(state, dispatch);
          }
        }
        return wrapInList(this.type)(state, dispatch);
      },
    };
  },
  addInputRules() {
    return [
      wrappingInputRule({
        find: /^\s*([-+*])\s$/,
        type: this.type,
      }),
    ];
  },
});

const OrderedListNode = Node.create({
  name: 'orderedList',
  group: 'block',
  content: 'listItem+',
  addAttributes() {
    return {
      start: {
        default: 1,
        parseHTML: (el) => Number(el.getAttribute('start') || 1),
      },
    };
  },
  parseHTML() {
    return [{ tag: 'ol' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['ol', mergeAttributes(HTMLAttributes), 0];
  },
  addCommands() {
    return {
      toggleOrderedList: () => ({ state, dispatch }) => {
        const { $from } = state.selection;
        for (let d = $from.depth; d > 0; d -= 1) {
          if ($from.node(d).type === this.type) {
            return liftListItem(state.schema.nodes.listItem)(state, dispatch);
          }
        }
        return wrapInList(this.type)(state, dispatch);
      },
    };
  },
  addInputRules() {
    return [
      wrappingInputRule({
        find: /^(\d+)\.\s$/,
        type: this.type,
        getAttributes: (match) => ({ start: Number(match[1] || 1) }),
        joinPredicate: (match, node) => node.childCount + node.attrs.start === Number(match[1]),
      }),
    ];
  },
});

const ListItemNode = Node.create({
  name: 'listItem',
  content: 'paragraph block*',
  defining: true,
  parseHTML() {
    return [{ tag: 'li:not([data-checked])' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['li', mergeAttributes(HTMLAttributes), 0];
  },
  addKeyboardShortcuts() {
    return {
      Enter: () => splitListItem(this.type)(this.editor.state, this.editor.view.dispatch),
      Tab: () => sinkListItem(this.type)(this.editor.state, this.editor.view.dispatch),
      'Shift-Tab': () => liftListItem(this.type)(this.editor.state, this.editor.view.dispatch),
    };
  },
});

const TaskListNode = Node.create({
  name: 'taskList',
  group: 'block',
  content: 'taskItem+',
  parseHTML() {
    return [{ tag: 'ul[data-type="taskList"]' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['ul', mergeAttributes(HTMLAttributes, { 'data-type': 'taskList', class: 'sy-task-list' }), 0];
  },
  addCommands() {
    return {
      toggleTaskList: () => ({ chain, state }) => {
        const { $from } = state.selection;
        for (let d = $from.depth; d > 0; d -= 1) {
          if ($from.node(d).type === this.type) {
            return liftListItem(state.schema.nodes.taskItem)(state, this.editor.view.dispatch);
          }
        }
        const text = $from.parent.textContent || '';
        return chain()
          .command(({ tr, dispatch }) => {
            if (!dispatch) return true;
            const blockStart = $from.before($from.depth);
            const blockEnd = $from.after($from.depth);
            const taskNode = state.schema.nodes.taskList.create(null, [
              state.schema.nodes.taskItem.create(
                { checked: false },
                [state.schema.nodes.paragraph.create(null, text ? [state.schema.text(text)] : [])],
              ),
            ]);
            tr.replaceWith(blockStart, blockEnd, taskNode);
            tr.setSelection(TextSelection.near(tr.doc.resolve(Math.min(blockStart + 3 + text.length, tr.doc.content.size))));
            return true;
          })
          .run();
      },
    };
  },
});

const TaskItemNode = Node.create({
  name: 'taskItem',
  content: 'paragraph block*',
  defining: true,
  addAttributes() {
    return {
      checked: {
        default: false,
        parseHTML: (el) => el.getAttribute('data-checked') === 'true',
        renderHTML: (attrs) => ({ 'data-checked': attrs.checked ? 'true' : 'false' }),
      },
    };
  },
  parseHTML() {
    return [{ tag: 'li[data-checked]' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['li', mergeAttributes(HTMLAttributes, { class: 'sy-task-item' }), 0];
  },
  addNodeView() {
    return VueNodeViewRenderer(NoteTaskItemView);
  },
  addInputRules() {
    return [
      new InputRule({
        find: /^(?:[-+*]\s+)?\[([ xX])\]\s$/,
        handler: ({ state, range, match }) => {
          const checked = match[1].toLowerCase() === 'x';
          const { tr, schema } = state;
          const $from = state.doc.resolve(range.from);
          if ($from.depth >= 2 && $from.node($from.depth - 1).type.name === 'bulletList') {
            const listPos = $from.before($from.depth - 2);
            const listNode = $from.node($from.depth - 2);
            if (listNode.childCount === 1) {
              const taskList = schema.nodes.taskList.create(null, [
                schema.nodes.taskItem.create({ checked }, [schema.nodes.paragraph.create()]),
              ]);
              tr.replaceWith(listPos, listPos + listNode.nodeSize, taskList);
              tr.setSelection(TextSelection.near(tr.doc.resolve(listPos + 3)));
              return;
            }
          }
          const blockStart = $from.before($from.depth);
          const blockEnd = $from.after($from.depth);
          const taskList = schema.nodes.taskList.create(null, [
            schema.nodes.taskItem.create({ checked }, [schema.nodes.paragraph.create()]),
          ]);
          tr.replaceWith(blockStart, blockEnd, taskList);
          tr.setSelection(TextSelection.near(tr.doc.resolve(blockStart + 3)));
        },
      }),
    ];
  },
  addKeyboardShortcuts() {
    return {
      Enter: () => splitListItem(this.type)(this.editor.state, this.editor.view.dispatch),
      Tab: () => sinkListItem(this.type)(this.editor.state, this.editor.view.dispatch),
      'Shift-Tab': () => liftListItem(this.type)(this.editor.state, this.editor.view.dispatch),
    };
  },
});

const CodeHighlightPluginKey = new PluginKey('syCodeHighlight');

const CodeBlockNode = Node.create({
  name: 'codeBlock',
  group: 'block',
  content: 'text*',
  marks: '',
  code: true,
  defining: true,
  addAttributes() {
    return {
      language: {
        default: '',
        parseHTML: (el) => {
          const cls = el.querySelector('code')?.className || el.className || '';
          const m = cls.match(/language-([a-zA-Z0-9_+-]+)/);
          return m ? m[1].toLowerCase() : '';
        },
      },
    };
  },
  parseHTML() {
    return [{ tag: 'pre', preserveWhitespace: 'full' }];
  },
  renderHTML({ node, HTMLAttributes }) {
    const lang = node.attrs.language ? `language-${node.attrs.language}` : '';
    return ['pre', mergeAttributes(HTMLAttributes), ['code', lang ? { class: lang } : {}, 0]];
  },
  addNodeView() {
    return VueNodeViewRenderer(NoteCodeBlockView);
  },
  addCommands() {
    return {
      setCodeBlock: (attrs = {}) => ({ state, dispatch }) => setBlockType(this.type, attrs)(state, dispatch),
      toggleCodeBlock: (attrs = {}) => ({ state, dispatch }) => {
        const { $from } = state.selection;
        if ($from.parent.type === this.type) {
          return setBlockType(state.schema.nodes.paragraph)(state, dispatch);
        }
        return setBlockType(this.type, attrs)(state, dispatch);
      },
    };
  },
  addInputRules() {
    return [
      textblockTypeInputRule({
        find: /^```([a-zA-Z0-9_+-]*)[\s\n]$/,
        type: this.type,
        getAttributes: (match) => ({ language: String(match[1] || '').toLowerCase() }),
      }),
    ];
  },
  addKeyboardShortcuts() {
    return {
      Tab: ({ editor }) => {
        if (!editor.isActive('codeBlock')) return false;
        editor.commands.insertContent('  ');
        return true;
      },
      Enter: ({ editor }) => {
        const { state } = editor;
        const { selection } = state;
        const { $from, empty } = selection;
        if (!empty || $from.parent.type !== this.type) return false;
        const isAtEnd = $from.parentOffset === $from.parent.nodeSize - 2;
        const endsWithDoubleNewline = $from.parent.textContent.endsWith('\n\n');
        if (isAtEnd && endsWithDoubleNewline) {
          return editor
            .chain()
            .command(({ tr }) => {
              tr.delete($from.pos - 2, $from.pos);
              return true;
            })
            .command(({ state: s, dispatch }) => exitCode(s, dispatch))
            .run();
        }
        return newlineInCode(state, editor.view.dispatch);
      },
      'Mod-Enter': ({ editor }) => {
        if (!editor.isActive('codeBlock')) return false;
        return exitCode(editor.state, editor.view.dispatch);
      },
      ArrowDown: ({ editor }) => {
        const { state } = editor;
        const { $from, empty } = state.selection;
        if (!empty || $from.parent.type !== this.type) return false;
        const atDocEnd = $from.after($from.depth) === state.doc.content.size;
        const atBlockEnd = $from.parentOffset === $from.parent.nodeSize - 2;
        if (atDocEnd && atBlockEnd) {
          return exitCode(state, editor.view.dispatch);
        }
        return false;
      },
    };
  },
  addProseMirrorPlugins() {
    return [
      new Plugin({
        key: CodeHighlightPluginKey,
        state: {
          init: (_, { doc }) => buildCodeHighlightDecorations(doc),
          apply: (tr, oldSet) => (tr.docChanged ? buildCodeHighlightDecorations(tr.doc) : oldSet),
        },
        props: {
          decorations(state) {
            return this.getState(state);
          },
        },
      }),
    ];
  },
});

const ImageBlockNode = Node.create({
  name: 'imageBlock',
  group: 'block',
  atom: true,
  draggable: true,
  selectable: true,
  addStorage() {
    return {
      onPreview: null,
    };
  },
  addAttributes() {
    return {
      src: { default: '' },
      alt: { default: '' },
      title: { default: '' },
    };
  },
  parseHTML() {
    return [
      {
        tag: 'figure[data-image-block] img',
        getAttrs: (el) => ({
          src: el.getAttribute('src') || '',
          alt: el.getAttribute('alt') || '',
          title: el.getAttribute('title') || '',
        }),
      },
    ];
  },
  renderHTML({ HTMLAttributes }) {
    return ['figure', { 'data-image-block': '' }, ['img', mergeAttributes(HTMLAttributes)]];
  },
  addNodeView() {
    return VueNodeViewRenderer(NoteImageBlockView);
  },
  addCommands() {
    return {
      insertImageBlock: (attrs) => ({ chain }) =>
        chain()
          .insertContent([
            { type: this.name, attrs },
            { type: 'paragraph' },
          ])
          .run(),
    };
  },
});

// Markdown the editor does not model (math, front matter) is kept verbatim
// and edited in source mode.
const RawBlockNode = Node.create({
  name: 'rawBlock',
  group: 'block',
  atom: true,
  selectable: true,
  draggable: true,
  addAttributes() {
    return {
      source: { default: '', rendered: false },
      kind: { default: '', rendered: false },
    };
  },
  parseHTML() {
    return [
      {
        tag: 'div[data-raw-block]',
        priority: 100,
        getAttrs: (el) => ({ source: el.textContent || '', kind: el.getAttribute('data-raw-kind') || '' }),
      },
    ];
  },
  renderHTML({ node }) {
    return [
      'div',
      {
        'data-raw-block': '',
        'data-raw-kind': node.attrs.kind || '',
        class: 'sy-raw-block',
        title: node.attrs.kind === 'frontmatter' ? '文档元数据（在源码模式中编辑）' : '公式（在源码模式中编辑）',
      },
      ['pre', {}, node.attrs.source || ''],
    ];
  },
});

const RawInlineNode = Node.create({
  name: 'rawInline',
  group: 'inline',
  inline: true,
  atom: true,
  selectable: true,
  addAttributes() {
    return { source: { default: '', rendered: false } };
  },
  parseHTML() {
    return [
      {
        tag: 'span[data-raw-inline]',
        priority: 100,
        getAttrs: (el) => ({ source: el.textContent || '' }),
      },
    ];
  },
  renderHTML({ node }) {
    return ['span', { 'data-raw-inline': '', class: 'sy-raw-inline', title: '公式（在源码模式中编辑）' }, node.attrs.source || ''];
  },
});

const InlineImageNode = Node.create({
  name: 'inlineImage',
  group: 'inline',
  inline: true,
  atom: true,
  selectable: true,
  addAttributes() {
    return {
      src: { default: '' },
      alt: { default: '' },
      title: { default: '' },
    };
  },
  parseHTML() {
    return [
      {
        tag: 'img[src]',
        getAttrs: (el) => ({
          src: el.getAttribute('src') || '',
          alt: el.getAttribute('alt') || '',
          title: el.getAttribute('title') || '',
        }),
      },
    ];
  },
  renderHTML({ HTMLAttributes }) {
    return ['img', mergeAttributes(HTMLAttributes, { class: 'sy-inline-image' })];
  },
});

const ReferenceInlineNode = Node.create({
  name: 'reference',
  group: 'inline',
  inline: true,
  atom: true,
  selectable: true,
  draggable: false,
  addStorage() {
    return { invalid: shallowReactive(new Set()) };
  },
  addAttributes() {
    return Object.fromEntries(
      ['kind', 'id', 'label', 'scope', 'turns', 'itemId', 'mode', 'modeReason', 'bodyChars', 'contentLimit'].map(
        (key) => [key, { default: key === 'scope' ? 'full' : '' }],
      ),
    );
  },
  parseHTML() {
    return [
      {
        tag: 'span[data-reference]',
        getAttrs: (element) =>
          referenceFromUrl(
            element.getAttribute('data-reference'),
            element.getAttribute('data-reference-label') || element.textContent,
          ),
      },
    ];
  },
  renderHTML({ node }) {
    return [
      'span',
      { 'data-reference': referenceUrl(node.attrs), class: 'reference-inline-node' },
      node.attrs.label,
    ];
  },
  addNodeView() {
    return VueNodeViewRenderer(ReferenceNodeView);
  },
});

function createCellAttributes() {
  return {
    colspan: {
      default: 1,
      parseHTML: (el) => Number(el.getAttribute('colspan') || 1),
    },
    rowspan: {
      default: 1,
      parseHTML: (el) => Number(el.getAttribute('rowspan') || 1),
    },
    colwidth: {
      default: null,
      parseHTML: (el) => {
        const w = el.getAttribute('data-colwidth');
        return w ? w.split(',').map((n) => Number(n)) : null;
      },
    },
    textAlign: {
      default: null,
      parseHTML: (el) => el.style?.textAlign || el.getAttribute('align') || null,
      renderHTML: (attrs) => (attrs.textAlign ? { style: `text-align: ${attrs.textAlign}` } : {}),
    },
  };
}

const TableNode = Node.create({
  name: 'table',
  group: 'block',
  content: 'tableRow+',
  tableRole: 'table',
  isolating: true,
  extendNodeSchema(extension) {
    const role = typeof extension.config.tableRole === 'function'
      ? extension.config.tableRole.call(extension)
      : extension.config.tableRole;
    return role ? { tableRole: role } : {};
  },
  parseHTML() {
    return [{ tag: 'table' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['div', { class: 'sy-table-scroll' }, ['table', mergeAttributes(HTMLAttributes, { class: 'sy-table' }), ['tbody', 0]]];
  },
  addCommands() {
    return {
      insertTable: ({ rows = 3, cols = 3, withHeaderRow = true } = {}) => ({ tr, dispatch, state }) => {
        const { schema } = state;
        const rowNodes = [];
        for (let r = 0; r < rows; r += 1) {
          const cellType = withHeaderRow && r === 0 ? schema.nodes.tableHeader : schema.nodes.tableCell;
          const cells = [];
          for (let c = 0; c < cols; c += 1) {
            cells.push(cellType.create(null, [schema.nodes.paragraph.create()]));
          }
          rowNodes.push(schema.nodes.tableRow.create(null, cells));
        }
        const table = schema.nodes.table.create(null, rowNodes);
        if (dispatch) {
          const pos = tr.selection.from;
          tr.replaceSelectionWith(table);
          const resolved = tr.doc.resolve(Math.min(pos + 4, tr.doc.content.size));
          tr.setSelection(TextSelection.near(resolved));
        }
        return true;
      },
      addColumnBefore: () => ({ state, dispatch }) => addColumnBefore(state, dispatch),
      addColumnAfter: () => ({ state, dispatch }) => addColumnAfter(state, dispatch),
      deleteColumn: () => ({ state, dispatch }) => deleteColumn(state, dispatch),
      addRowBefore: () => ({ state, dispatch }) => addRowBefore(state, dispatch),
      addRowAfter: () => ({ state, dispatch }) => addRowAfter(state, dispatch),
      deleteRow: () => ({ state, dispatch }) => deleteRow(state, dispatch),
      deleteTable: () => ({ state, dispatch }) => deleteTable(state, dispatch),
      toggleHeaderRow: () => ({ state, dispatch }) => toggleHeaderRow(state, dispatch),
      setColumnAlignment: (align) => ({ state, dispatch }) => {
        if (!isInTable(state)) return false;
        const rect = selectedRect(state);
        const map = TableMap.get(rect.table);
        if (dispatch) {
          const tr = state.tr;
          for (let row = 0; row < map.height; row += 1) {
            for (let col = rect.left; col < rect.right; col += 1) {
              const cellPos = map.positionAt(row, col, rect.table);
              const cell = rect.table.nodeAt(cellPos);
              if (cell) {
                tr.setNodeMarkup(rect.tableStart + cellPos, null, {
                  ...cell.attrs,
                  textAlign: align,
                });
              }
            }
          }
          dispatch(tr);
        }
        return true;
      },
    };
  },
  addKeyboardShortcuts() {
    return {
      Tab: () => goToNextCell(1)(this.editor.state, this.editor.view.dispatch),
      'Shift-Tab': () => goToNextCell(-1)(this.editor.state, this.editor.view.dispatch),
    };
  },
  addProseMirrorPlugins() {
    return [
      columnResizing({ handleWidth: 5, cellMinWidth: 56 }),
      tableEditing(),
      new Plugin({
        appendTransaction(_transactions, oldState, state) {
          if (state.doc === oldState.doc) return null;
          return fixTables(state) || null;
        },
      }),
    ];
  },
});

const TableRowNode = Node.create({
  name: 'tableRow',
  content: '(tableCell | tableHeader)+',
  tableRole: 'row',
  parseHTML() {
    return [{ tag: 'tr' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['tr', mergeAttributes(HTMLAttributes), 0];
  },
});

const TableHeaderNode = Node.create({
  name: 'tableHeader',
  content: 'block+',
  tableRole: 'header_cell',
  isolating: true,
  addAttributes() {
    return createCellAttributes();
  },
  parseHTML() {
    return [{ tag: 'th' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['th', mergeAttributes(HTMLAttributes), 0];
  },
});

const TableCellNode = Node.create({
  name: 'tableCell',
  content: 'block+',
  tableRole: 'cell',
  isolating: true,
  addAttributes() {
    return createCellAttributes();
  },
  parseHTML() {
    return [{ tag: 'td' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['td', mergeAttributes(HTMLAttributes), 0];
  },
});

const BoldMark = Mark.create({
  name: 'bold',
  parseHTML() {
    return [{ tag: 'strong' }, { tag: 'b' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['strong', mergeAttributes(HTMLAttributes), 0];
  },
  addCommands() {
    return {
      toggleBold: () => ({ state, dispatch }) => toggleMark(this.type)(state, dispatch),
    };
  },
  addInputRules() {
    return [
      markInputRule({ find: /(?:^|\s)(\*\*(?!\s+\*\*)((?:[^*]+))\*\*(?!\*))$/, type: this.type }),
      markInputRule({ find: /(?:^|\s)(__(?!\s+__)((?:[^_]+))__(?!_))$/, type: this.type }),
    ];
  },
  addKeyboardShortcuts() {
    return {
      'Mod-b': () => this.editor.commands.toggleBold(),
    };
  },
});

const ItalicMark = Mark.create({
  name: 'italic',
  parseHTML() {
    return [{ tag: 'em' }, { tag: 'i' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['em', mergeAttributes(HTMLAttributes), 0];
  },
  addCommands() {
    return {
      toggleItalic: () => ({ state, dispatch }) => toggleMark(this.type)(state, dispatch),
    };
  },
  addInputRules() {
    return [
      markInputRule({ find: /(?:^|[^*])(\*(?!\s+\*)((?:[^*]+))\*(?!\*))$/, type: this.type }),
    ];
  },
  addKeyboardShortcuts() {
    return {
      'Mod-i': () => this.editor.commands.toggleItalic(),
    };
  },
});

const StrikeMark = Mark.create({
  name: 'strike',
  parseHTML() {
    return [{ tag: 's' }, { tag: 'del' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['s', mergeAttributes(HTMLAttributes), 0];
  },
  addCommands() {
    return {
      toggleStrike: () => ({ state, dispatch }) => toggleMark(this.type)(state, dispatch),
    };
  },
  addInputRules() {
    return [
      markInputRule({ find: /(?:^|\s)(~~(?!\s+~~)((?:[^~]+))~~(?!~))$/, type: this.type }),
    ];
  },
  addKeyboardShortcuts() {
    return {
      'Mod-Shift-x': () => this.editor.commands.toggleStrike(),
    };
  },
});

const CodeInlineMark = Mark.create({
  name: 'code',
  excludes: '_',
  code: true,
  parseHTML() {
    return [{ tag: 'code' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['code', mergeAttributes(HTMLAttributes, { class: 'sy-inline-code' }), 0];
  },
  addCommands() {
    return {
      toggleCode: () => ({ state, dispatch }) => toggleMark(this.type)(state, dispatch),
    };
  },
  addInputRules() {
    return [
      markInputRule({ find: /(?:^|[^`])(`(?!\s+`)([^`]+)`(?!`))$/, type: this.type }),
    ];
  },
  addKeyboardShortcuts() {
    return {
      'Mod-e': () => this.editor.commands.toggleCode(),
    };
  },
});

const LinkMark = Mark.create({
  name: 'link',
  inclusive: false,
  addAttributes() {
    return {
      href: { default: null },
      title: { default: null },
    };
  },
  parseHTML() {
    return [
      {
        tag: 'a[href]',
        getAttrs: (el) => {
          const href = safeLinkHref(el.getAttribute('href'));
          return href ? { href, title: el.getAttribute('title') } : false;
        },
      },
    ];
  },
  renderHTML({ HTMLAttributes }) {
    const { href, ...rest } = HTMLAttributes;
    const safe = safeLinkHref(href);
    return [
      'a',
      mergeAttributes(rest, safe ? { href: safe } : {}, {
        class: 'sy-link',
        rel: 'noopener noreferrer',
        target: '_blank',
      }),
      0,
    ];
  },
  addCommands() {
    return {
      setLink: (attrs) => ({ state, dispatch }) => {
        const href = safeLinkHref(attrs?.href);
        return href ? toggleMark(this.type, { ...attrs, href })(state, dispatch) : false;
      },
      unsetLink: () => ({ tr, state, dispatch }) => {
        const { from, to } = state.selection;
        if (dispatch) tr.removeMark(from, to, this.type);
        return true;
      },
    };
  },
  addInputRules() {
    return [
      new InputRule({
        find: /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)\s$/,
        handler: ({ state, range, match }) => {
          const [, label, href] = match;
          const { tr, schema } = state;
          const mark = schema.marks.link.create({ href });
          tr.replaceWith(range.from, range.to, [
            schema.text(label, [mark]),
            schema.text(' '),
          ]);
        },
      }),
    ];
  },
});

const QuickTableInputExtension = Extension.create({
  name: 'syQuickTableAndHistory',
  addProseMirrorPlugins() {
    const getSelectedBlock = (state) => {
      const sel = blockSelectionPluginKey.getState(state);
      if (!sel || typeof sel.pos !== 'number') return null;
      const node = state.doc.nodeAt(sel.pos);
      if (!node) return null;
      return { pos: sel.pos, endPos: sel.pos + node.nodeSize, node };
    };

    const blockSelectionPlugin = new Plugin({
      key: blockSelectionPluginKey,
      state: {
        init: () => null,
        apply(tr, prev, _oldState, newState) {
          const meta = tr.getMeta(blockSelectionPluginKey);
          if (meta) {
            if (meta.clear) return null;
            if (typeof meta.pos === 'number') {
              const n = newState.doc.nodeAt(meta.pos);
              return n ? { pos: meta.pos } : null;
            }
          }
          if (!prev) return null;
          if (tr.docChanged) {
            const mapped = tr.mapping.map(prev.pos, 1);
            const n = newState.doc.nodeAt(mapped);
            return n ? { pos: mapped } : null;
          }
          if (tr.selectionSet && !tr.getMeta('keepBlockSelection')) {
            return null;
          }
          return prev;
        },
      },
      props: {
        decorations(state) {
          const active = getSelectedBlock(state);
          if (!active) return DecorationSet.empty;
          return DecorationSet.create(state.doc, [
            Decoration.node(active.pos, active.endPos, {
              class: 'sy-block-selected',
              'data-block-selected': 'true',
            }),
          ]);
        },
        handleDOMEvents: {
          mousedown(view, event) {
            if (event.button === 0 && blockSelectionPluginKey.getState(view.state)) {
              view.dispatch(view.state.tr.setMeta(blockSelectionPluginKey, { clear: true }));
            }
            return false;
          },
          copy(view, event) {
            const active = getSelectedBlock(view.state);
            if (!active) return false;
            const md = serializeSingleBlockMarkdown(active.node);
            if (event.clipboardData) {
              event.preventDefault();
              event.clipboardData.setData('text/plain', md);
              event.clipboardData.setData('text/markdown', md);
              return true;
            }
            return false;
          },
          cut(view, event) {
            const active = getSelectedBlock(view.state);
            if (!active || !view.editable) return false;
            const md = serializeSingleBlockMarkdown(active.node);
            if (event.clipboardData) {
              event.preventDefault();
              event.clipboardData.setData('text/plain', md);
              event.clipboardData.setData('text/markdown', md);
              deleteBlockAtPos(view.state, view.dispatch, active.pos);
              return true;
            }
            return false;
          },
        },
        handleKeyDown(view, event) {
          const active = getSelectedBlock(view.state);
          if (!active) return false;

          if (event.key === 'Escape') {
            event.preventDefault();
            view.dispatch(view.state.tr.setMeta(blockSelectionPluginKey, { clear: true }));
            return true;
          }

          if ((event.key === 'Backspace' || event.key === 'Delete') && view.editable) {
            event.preventDefault();
            return deleteBlockAtPos(view.state, view.dispatch, active.pos);
          }

          if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === 'c') {
            event.preventDefault();
            const md = serializeSingleBlockMarkdown(active.node);
            navigator.clipboard?.writeText?.(md).catch(() => {});
            return true;
          }

          if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === 'x' && view.editable) {
            event.preventDefault();
            const md = serializeSingleBlockMarkdown(active.node);
            navigator.clipboard?.writeText?.(md).catch(() => {});
            return deleteBlockAtPos(view.state, view.dispatch, active.pos);
          }

          if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === 'd' && view.editable) {
            event.preventDefault();
            return duplicateBlockAtPos(view.state, view.dispatch, active.pos);
          }

          if (event.altKey && !event.metaKey && !event.ctrlKey && event.key === 'ArrowUp' && view.editable) {
            event.preventDefault();
            return moveBlockByOffset(view.state, view.dispatch, active.pos, -1);
          }

          if (event.altKey && !event.metaKey && !event.ctrlKey && event.key === 'ArrowDown' && view.editable) {
            event.preventDefault();
            return moveBlockByOffset(view.state, view.dispatch, active.pos, 1);
          }

          if (event.key === 'Enter' && !event.shiftKey && view.editable) {
            event.preventDefault();
            return insertParagraphAroundBlock(view.state, view.dispatch, active.pos, 'after');
          }

          return false;
        },
      },
    });

    return [
      blockSelectionPlugin,
      history(),
      dropCursor({ color: 'var(--ob-blue, #3b82f6)', width: 2 }),
      gapCursor(),
    ];
  },
  addCommands() {
    return {
      selectBlockAt: (pos) => ({ state, dispatch }) => {
        const node = state.doc.nodeAt(pos);
        if (!node) return false;
        if (dispatch) {
          const tr = state.tr.setMeta(blockSelectionPluginKey, { pos }).setMeta('keepBlockSelection', true);
          dispatch(tr);
        }
        return true;
      },
      clearBlockSelection: () => ({ state, dispatch }) => {
        if (dispatch) {
          dispatch(state.tr.setMeta(blockSelectionPluginKey, { clear: true }));
        }
        return true;
      },
      deleteBlockAt: (pos) => ({ state, dispatch }) => deleteBlockAtPos(state, dispatch, pos),
      clearInlineMarks: () => ({ state, tr, dispatch }) => {
        const { from, to, empty } = state.selection;
        if (empty) return false;
        if (dispatch) {
          for (const markType of Object.values(state.schema.marks)) {
            tr.removeMark(from, to, markType);
          }
          dispatch(tr);
        }
        return true;
      },
      undoHistory: () => ({ state, dispatch }) => undo(state, dispatch),
      redoHistory: () => ({ state, dispatch }) => redo(state, dispatch),
      indentCurrent: () => ({ state, dispatch }) => {
        const { listItem, taskItem, blockquote } = state.schema.nodes;
        if (sinkListItem(listItem)(state, dispatch)) return true;
        if (sinkListItem(taskItem)(state, dispatch)) return true;
        return wrapIn(blockquote)(state, dispatch);
      },
      outdentCurrent: () => ({ state, dispatch }) => {
        const { listItem, taskItem } = state.schema.nodes;
        if (liftListItem(listItem)(state, dispatch)) return true;
        if (liftListItem(taskItem)(state, dispatch)) return true;
        return liftEmptyBlock(state, dispatch);
      },
    };
  },
  addKeyboardShortcuts() {
    return {
      'Mod-z': () => undo(this.editor.state, this.editor.view.dispatch),
      'Mod-Shift-z': () => redo(this.editor.state, this.editor.view.dispatch),
      'Mod-y': () => redo(this.editor.state, this.editor.view.dispatch),
      Enter: ({ editor }) => {
        const { state } = editor;
        const { $from, empty } = state.selection;
        if (!empty || $from.parent.type.name !== 'paragraph') return false;
        const line = $from.parent.textContent.trim();
        if (/^\|(.+)\|$/.test(line) && line.includes('|')) {
          const headers = line
            .slice(1, -1)
            .split('|')
            .map((s) => s.trim());
          if (headers.length >= 1 && headers.length <= 20) {
            const { schema } = state;
            const headerCells = headers.map((h) =>
              schema.nodes.tableHeader.create(
                null,
                [schema.nodes.paragraph.create(null, h ? [schema.text(h)] : [])],
              ),
            );
            const bodyCells = headers.map(() =>
              schema.nodes.tableCell.create(null, [schema.nodes.paragraph.create()]),
            );
            const table = schema.nodes.table.create(null, [
              schema.nodes.tableRow.create(null, headerCells),
              schema.nodes.tableRow.create(null, bodyCells),
            ]);
            const blockStart = $from.before($from.depth);
            const blockEnd = $from.after($from.depth);
            const tr = state.tr.replaceWith(blockStart, blockEnd, table);
            const firstBodyCellPos = blockStart + table.child(0).nodeSize + 3;
            tr.setSelection(TextSelection.near(tr.doc.resolve(Math.min(firstBodyCellPos, tr.doc.content.size))));
            editor.view.dispatch(tr);
            return true;
          }
        }
        return chainCommands(liftEmptyBlock, splitBlock)(state, editor.view.dispatch);
      },
    };
  },
});

const UnderlineMark = Mark.create({
  name: 'underline',
  parseHTML() {
    return [{ tag: 'u' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['u', mergeAttributes(HTMLAttributes), 0];
  },
  addCommands() {
    return {
      toggleUnderline: () => ({ state, dispatch }) => toggleMark(this.type)(state, dispatch),
    };
  },
  addKeyboardShortcuts() {
    return {
      'Mod-u': () => this.editor.commands.toggleUnderline(),
    };
  },
});

const HighlightMark = Mark.create({
  name: 'highlight',
  parseHTML() {
    return [{ tag: 'mark' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['mark', mergeAttributes(HTMLAttributes, { class: 'sy-mark-highlight' }), 0];
  },
  addCommands() {
    return {
      toggleHighlight: () => ({ state, dispatch }) => toggleMark(this.type)(state, dispatch),
    };
  },
  addInputRules() {
    return [
      markInputRule({ find: /(?:^|\s)(==(?!\s+==)((?:[^=]+))==(?!=))$/, type: this.type }),
    ];
  },
  addKeyboardShortcuts() {
    return {
      'Mod-Shift-h': () => this.editor.commands.toggleHighlight(),
    };
  },
});

const SuperscriptMark = Mark.create({
  name: 'superscript',
  excludes: 'subscript',
  parseHTML() {
    return [{ tag: 'sup' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['sup', mergeAttributes(HTMLAttributes), 0];
  },
  addCommands() {
    return {
      toggleSuperscript: () => ({ state, dispatch }) => toggleMark(this.type)(state, dispatch),
    };
  },
});

const SubscriptMark = Mark.create({
  name: 'subscript',
  excludes: 'superscript',
  parseHTML() {
    return [{ tag: 'sub' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['sub', mergeAttributes(HTMLAttributes), 0];
  },
  addCommands() {
    return {
      toggleSubscript: () => ({ state, dispatch }) => toggleMark(this.type)(state, dispatch),
    };
  },
});

const KbdMark = Mark.create({
  name: 'kbd',
  parseHTML() {
    return [{ tag: 'kbd' }];
  },
  renderHTML({ HTMLAttributes }) {
    return ['kbd', mergeAttributes(HTMLAttributes, { class: 'sy-inline-kbd' }), 0];
  },
  addCommands() {
    return {
      toggleKbd: () => ({ state, dispatch }) => toggleMark(this.type)(state, dispatch),
    };
  },
  addKeyboardShortcuts() {
    return {
      'Mod-Shift-k': () => this.editor.commands.toggleKbd(),
    };
  },
});

const InlineMemoMark = Mark.create({
  name: 'inlineMemo',
  inclusive: false,
  addAttributes() {
    return {
      memo: { default: '' },
    };
  },
  parseHTML() {
    return [
      {
        tag: 'span[data-inline-memo]',
        getAttrs: (el) => ({ memo: el.getAttribute('data-inline-memo') || '' }),
      },
      {
        tag: 'span[data-type~="inline-memo"]',
        getAttrs: (el) => ({ memo: el.getAttribute('data-inline-memo-content') || '' }),
      },
    ];
  },
  renderHTML({ HTMLAttributes }) {
    const memo = String(HTMLAttributes.memo || '');
    return [
      'span',
      {
        class: 'sy-inline-memo',
        'data-inline-memo': memo,
      },
      0,
    ];
  },
  addCommands() {
    return {
      setInlineMemo: (memo = '') => ({ tr, state, dispatch }) => {
        const { from, to, empty } = state.selection;
        if (empty) return false;
        if (dispatch) {
          tr.removeMark(from, to, this.type);
          tr.addMark(from, to, this.type.create({ memo }));
          dispatch(tr);
        }
        return true;
      },
      unsetInlineMemo: () => ({ tr, state, dispatch }) => {
        const { from, to } = state.selection;
        if (dispatch) {
          tr.removeMark(from, to, this.type);
          dispatch(tr);
        }
        return true;
      },
    };
  },
});

const TextStyleMark = Mark.create({
  name: 'textStyle',
  addAttributes() {
    return {
      color: { default: '' },
      bg: { default: '' },
    };
  },
  parseHTML() {
    return [
      {
        tag: 'span[data-color], span[data-bg]',
        getAttrs: (el) => ({
          color: el.getAttribute('data-color') || '',
          bg: el.getAttribute('data-bg') || '',
        }),
      },
    ];
  },
  renderHTML({ HTMLAttributes }) {
    const color = String(HTMLAttributes.color || '');
    const bg = String(HTMLAttributes.bg || '');
    const styleParts = [];
    if (color) styleParts.push(`color: ${color}`);
    if (bg) styleParts.push(`background-color: ${bg}; border-radius: 3px; padding: 0 2px`);
    const attrs = { class: 'sy-text-style' };
    if (color) attrs['data-color'] = color;
    if (bg) attrs['data-bg'] = bg;
    if (styleParts.length) attrs.style = styleParts.join('; ');
    return ['span', attrs, 0];
  },
  addCommands() {
    return {
      setTextStyleAppearance: ({ color, bg } = {}) => ({ state, tr, dispatch }) => {
        const { from, to, empty } = state.selection;
        if (empty) return false;
        let curColor = '';
        let curBg = '';
        state.doc.nodesBetween(from, to, (node) => {
          const existing = node.marks?.find?.((m) => m.type === this.type);
          if (existing) {
            if (existing.attrs?.color) curColor = existing.attrs.color;
            if (existing.attrs?.bg) curBg = existing.attrs.bg;
          }
        });
        const nextColor = color !== undefined ? color : curColor;
        const nextBg = bg !== undefined ? bg : curBg;
        if (dispatch) {
          tr.removeMark(from, to, this.type);
          if (nextColor || nextBg) {
            tr.addMark(from, to, this.type.create({ color: nextColor, bg: nextBg }));
          }
          dispatch(tr);
        }
        return true;
      },
    };
  },
});

export function createNoteEditorExtensions({ onImagePreview, onTriggerMemoShortcut } = {}) {
  return [
    DocumentNode,
    TextNode,
    ParagraphNode,
    HeadingNode,
    HardBreakNode,
    HorizontalRuleNode,
    BlockquoteNode,
    BulletListNode,
    OrderedListNode,
    ListItemNode,
    TaskListNode,
    TaskItemNode,
    CodeBlockNode,
    ImageBlockNode.configure ? ImageBlockNode : ImageBlockNode,
    Extension.create({
      name: 'syImageStorageBinding',
      onCreate() {
        if (this.editor.storage.imageBlock) {
          this.editor.storage.imageBlock.onPreview = onImagePreview || null;
        }
      },
      addKeyboardShortcuts() {
        return {
          'Mod-Alt-m': () => {
            if (onTriggerMemoShortcut) {
              return Boolean(onTriggerMemoShortcut());
            }
            return false;
          },
          'Mod-\\': () => this.editor.commands.clearInlineMarks(),
        };
      },
    }),
    RawBlockNode,
    RawInlineNode,
    InlineImageNode,
    ReferenceInlineNode,
    TableNode,
    TableRowNode,
    TableHeaderNode,
    TableCellNode,
    BoldMark,
    ItalicMark,
    UnderlineMark,
    StrikeMark,
    HighlightMark,
    SuperscriptMark,
    SubscriptMark,
    KbdMark,
    CodeInlineMark,
    InlineMemoMark,
    TextStyleMark,
    LinkMark,
    QuickTableInputExtension,
  ];
}
