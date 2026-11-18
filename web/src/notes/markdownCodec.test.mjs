import assert from 'node:assert/strict';
import test from 'node:test';
import hljs from 'highlight.js';
import {
  computeDocumentStats,
  docToMarkdown,
  extractOutlineFromMarkdown,
  looksLikeStructuredMarkdown,
  markdownToDoc,
  parseHljsHtmlToRanges,
  safeLinkHref,
} from './markdownCodec.js';

test('markdownCodec round-trips headings, lists, task lists, blockquotes, code fences, images, and @references', () => {
  const input = [
    '## 部署步骤',
    '',
    '来源：[RouterOS 排障 · 第 3 轮](openbear://ref/turn/conv-1/turn-3?mode=mention)',
    '',
    '> 核心交换设备不允许断网重启。',
    '',
    '- [x] 备份配置',
    '- [ ] 验证 WireGuard 路由',
    '',
    '```bash',
    'wg show all',
    'ip -4 route list table all',
    '```',
    '',
    '![拓扑图](/api/kb-notes/assets/abc123/topo.png)',
  ].join('\n');

  const doc = markdownToDoc(input);
  assert.equal(doc.type, 'doc');
  assert.ok(doc.content.some((n) => n.type === 'taskList'));
  assert.ok(doc.content.some((n) => n.type === 'codeBlock' && n.attrs.language === 'bash'));
  assert.ok(doc.content.some((n) => n.type === 'imageBlock' && n.attrs.alt === '拓扑图'));

  const output = docToMarkdown(doc);
  assert.equal(output, input);
});

test('markdownCodec round-trips GFM tables with column alignments and escaped pipes', () => {
  const tableMd = [
    '# WWAN 频段表',
    '',
    '| Band | Duplex | 下行频率 (MHz) | 备注 |',
    '| :--- | :---: | ---: | --- |',
    '| 1 | FDD | 2110–2170 | 主力\\|常用 |',
    '| 3 | FDD | 1805–1880 | 城市覆盖 |',
  ].join('\n');

  const doc = markdownToDoc(tableMd);
  const table = doc.content.find((n) => n.type === 'table');
  assert.ok(table);
  assert.equal(table.content.length, 3);
  assert.equal(table.content[0].content[1].attrs.textAlign, 'center');
  assert.equal(table.content[0].content[2].attrs.textAlign, 'right');

  const serialized = docToMarkdown(doc);
  assert.equal(serialized, tableMd);

  const outline = extractOutlineFromMarkdown(serialized);
  assert.deepEqual(outline, [{ index: 0, level: 1, text: 'WWAN 频段表' }]);
  const stats = computeDocumentStats('中文测试 hello 123');
  assert.equal(stats.words, 6);
  assert.equal(looksLikeStructuredMarkdown(tableMd), true);
});

test('parseHljsHtmlToRanges maps highlight.js spans accurately with HTML entities', () => {
  const code = 'if (a < 10 && b > 2) {\n  const s = "hi & bye";\n}';
  const highlighted = hljs.highlight(code, { language: 'javascript', ignoreIllegals: true });
  const ranges = parseHljsHtmlToRanges(highlighted.value, code.length);
  assert.ok(ranges.length > 0);
  for (const r of ranges) {
    assert.ok(r.from >= 0 && r.to <= code.length && r.from < r.to);
    assert.match(r.className, /^hljs-/);
  }
});

test('@note reference syntax, mention prefix, and Vue SFC templates compile cleanly', async () => {
  const fs = await import('node:fs');
  const { parse, compileTemplate } = await import('@vue/compiler-sfc');
  const { Schema } = await import('@tiptap/pm/model');
  const { TextSelection } = await import('@tiptap/pm/state');
  const { normalizeReference, referenceToken, referenceFromUrl } = await import('../references/codec.js');
  const { mentionAtPosition } = await import('../references/mention.js');
  const { referenceCandidateDetail, referenceChipOpen } = await import('../references/presentation.js');

  const noteRef = normalizeReference({ kind: 'note', id: 'f47ac10b58cc4372a5670e02b2c3d479', label: 'WWAN模块知识' });
  assert.ok(noteRef);
  // No tool can read a note for the model, so a note is never a locator-only mention.
  assert.equal(normalizeReference({ ...noteRef, mode: 'mention' }).mode, undefined);
  const token = referenceToken(noteRef);
  assert.equal(token, '[WWAN模块知识](openbear://ref/note/f47ac10b58cc4372a5670e02b2c3d479)');
  assert.deepEqual(referenceFromUrl('openbear://ref/note/f47ac10b58cc4372a5670e02b2c3d479', 'WWAN模块知识'), noteRef);
  assert.equal(referenceCandidateDetail({ kind: 'note', preview: '频段整理摘要' }), '频段整理摘要');
  assert.match(referenceChipOpen(noteRef), /reference-chip--note/);

  const schema = new Schema({
    nodes: {
      doc: { content: 'paragraph+' },
      paragraph: { content: 'inline*' },
      text: { group: 'inline' },
    },
  });
  const docNode = schema.nodeFromJSON({
    type: 'doc',
    content: [{ type: 'paragraph', content: [{ type: 'text', text: '帮我结合 @note/WWAN 分析' }] }],
  });
  const pos = docNode.resolve(1 + '帮我结合 @note/WWAN'.length);
  assert.deepEqual(mentionAtPosition(TextSelection.create(docNode, pos.pos).$from), {
    from: 1 + '帮我结合 '.length,
    to: 1 + '帮我结合 @note/WWAN'.length,
    query: 'WWAN',
    kind: 'note',
  });

  for (const rel of [
    './SiyuanLikeEditor.vue',
    './NoteImportDialog.vue',
    './NoteMoveDialog.vue',
    './NoteCaptureDialog.vue',
    './NoteCodeBlockView.vue',
    './NoteImageBlockView.vue',
    './NoteTaskItemView.vue',
    './NoteIcon.vue',
    './NoteBlockGutterIcon.vue',
    './NoteTopToolbar.vue',
    '../views/NotesView.vue',
  ]) {
    const src = fs.readFileSync(new URL(rel, import.meta.url), 'utf8');
    const descriptor = parse(src).descriptor;
    const compiled = compileTemplate({ source: descriptor.template.content, filename: rel, id: rel });
    assert.deepEqual(compiled.errors, [], `template compile errors in ${rel}`);
  }
});

test('opening a note never rewrites its source: front matter and a title-like H1 stay', () => {
  const input = [
    '---',
    'title: 网络相关命令',
    'tags: [net]',
    '---',
    '',
    '# 网络相关命令',
    '',
    '- 获取外网ip',
  ].join('\n');
  const doc = markdownToDoc(input);
  assert.deepEqual(doc.content.map((n) => n.type), ['rawBlock', 'heading', 'bulletList']);
  assert.equal(docToMarkdown(doc), input);
});

test('editing round-trips escapes, nesting, soft breaks and unsupported syntax without changing meaning', () => {
  const cases = [
    'Use \\*literal\\* stars and 2 \\* 3',
    '\\# 不是标题',
    '[a](https://x.com) and \\[not link\\](y)',
    '**bold *italic* end**',
    '**粗体 `code` 结尾**',
    '**“引号”**后面',
    'line1\nline2',
    'line1  \nline2',
    '<details>\n<summary>s</summary>\nbody\n</details>',
    '$$\nE=mc^2\n\\{a\\}\n$$',
    '质能 $E=mc^2$ 与 $a_1 * b_2$ 公式',
    '- 列表\n\n  $$\n  x^2\n  $$',
    '- [ ] 待办\n- [x] 完成',
    '- \\[ \\] 不是待办',
    '- 第一段\n\n  第二段\n- 下一项',
    '标题\n\\---\n文本',
    '2024\\. 年度总结',
    'if a == b == c',
    'approx \\~5 and \\~10 items',
    '| a | b |\n| --- | --- |\n| `x\\|y` | 2 |',
    '<span data-color="#e03131">**红色粗体**</span> 与 <u>*下划线斜体*</u>',
    '```bash\na\n\n\n\nb\n```',
    '- 项目\n\n  ```bash\n  x\n\n  y\n  ```',
    '| 名称 | 备注 |\n| --- | --- |\n| 钉钉 | [密钥]^(Client ID \\(原 AppKey\\)) |',
    '1. 第一行  \n   第二行',
    '> 见 [链接](https://x.y)  \n> 下一行',
    '![图](assets/中文-图片.png) [路径](/a%25b)',
  ];
  const sortMarks = (value) => JSON.parse(JSON.stringify(value), (key, v) => (
    key === 'marks' && Array.isArray(v) ? [...v].sort((a, b) => a.type.localeCompare(b.type)) : v
  ));
  for (const input of cases) {
    const first = markdownToDoc(input);
    const output = docToMarkdown(first);
    assert.deepEqual(sortMarks(markdownToDoc(output)), sortMarks(first), `meaning changed for ${JSON.stringify(input)} -> ${JSON.stringify(output)}`);
    assert.equal(docToMarkdown(markdownToDoc(output)), output, `unstable output for ${JSON.stringify(input)}`);
  }
  // Common inputs keep their exact source.
  for (const input of [...cases.slice(0, 18), ...cases.slice(-6)]) assert.equal(docToMarkdown(markdownToDoc(input)), input);

  // Typed text that looks like markup is escaped instead of turning into markup.
  const typed = { type: 'doc', content: [{ type: 'paragraph', content: [
    { type: 'text', text: '# 标题? *星号* [链接](x) 1. 列表 ==高亮== $a$ 和 $b$' },
  ] }] };
  const typedMd = docToMarkdown(typed);
  assert.deepEqual(markdownToDoc(typedMd).content, typed.content);
});

test('safeLinkHref refuses script-capable schemes and keeps ordinary links', () => {
  for (const ok of ['https://a.b/c', 'http://x', 'mailto:a@b.c', 'tel:123', '/api/x', '#h', 'notes/a.md', 'openbear://ref/note/x']) {
    assert.equal(safeLinkHref(ok), ok);
  }
  for (const bad of ['javascript:alert(1)', ' JavaScript:alert(1)', 'java\tscript:alert(1)', 'vbscript:x', 'data:text/html,<b>', 'file:///etc/passwd']) {
    assert.equal(safeLinkHref(bad), null, bad);
  }
});

test('inlineMemo and extended toolbar marks (underline, highlight, sup, sub, kbd, textStyle) round-trip cleanly in paragraphs and tables', () => {
  const mdInput = [
    '| 名称 | 密钥 | 备注 |',
    '| :---: | :---: | :---: |',
    '| OpenAI | [密钥]^(sk-demo-123\\n第二行\\|带竖线\\)右括号) | 账号说明 |',
    '',
    '支持 <u>下划线</u>、==高亮标记==、X^2^、H~2~O、<kbd>Ctrl+Alt+M</kbd> 与 <span data-color="#e03131">彩色文字</span>。',
  ].join('\n');

  const doc = markdownToDoc(mdInput);
  const tableCell = doc.content[0].content[1].content[1];
  const memoTextNode = tableCell.content[0].content[0];
  assert.equal(memoTextNode.text, '密钥');
  assert.deepEqual(memoTextNode.marks, [
    { type: 'inlineMemo', attrs: { memo: 'sk-demo-123\n第二行|带竖线)右括号' } },
  ]);

  const serialized = docToMarkdown(doc);
  assert.match(serialized, /\[密钥\]\^\(sk-demo-123\\n第二行\\\|带竖线\\\)右括号\)/);
  assert.match(serialized, /<u>下划线<\/u>/);
  assert.match(serialized, /==高亮标记==/);
  assert.match(serialized, /\^2\^/);
  assert.match(serialized, /~2~/);
  assert.match(serialized, /<kbd>Ctrl\+Alt\+M<\/kbd>/);
  assert.match(serialized, /<span data-color="#e03131">彩色文字<\/span>/);
});
