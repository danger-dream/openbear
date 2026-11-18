import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';
import { startBrowserTestServer } from '../browserTestServer.mjs';
import { createToolFileBackend, uuid } from '../toolFileOpenFixture.mjs';

const require = createRequire(import.meta.url);
let chromium;
try {
  ({ chromium } = require('../testing/playwright.cjs'));
} catch {}

const entry = `import '/src/monaco-worker.js'; import '/src/theme.js'; import {createApp} from 'vue'; import ElementPlus from 'element-plus'; import * as Icons from '@element-plus/icons-vue'; import 'element-plus/dist/index.css'; import 'element-plus/theme-chalk/dark/css-vars.css'; import '/src/style.css'; import '/src/dark-theme.css'; import '/src/admin-mobile.css'; import '/src/mobile-overlays.css'; import '/src/mobile-inputs.css'; import App from '/src/App.vue'; const app=createApp(App); app.use(ElementPlus); for(const [name,component] of Object.entries(Icons))app.component(name,component);app.mount('#app');`;
const html = '<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><script>window.__OPENBEAR_AUTH_MODE__="controller"</script></head><body><div id="app"></div><script type="module" src="/@id/__x00__virtual:kb-notes-test"></script></body></html>';

const FIDELITY_SOURCE = [
  '---',
  'tags: [net]',
  '---',
  '',
  '# 转义与公式',
  '',
  '字面 \\*星号\\* 与 **粗 *斜* 粗**',
  '',
  '\\# 不是标题',
  '',
  '第一行',
  '第二行',
  '',
  '$$',
  'E=mc^2',
  '$$',
  '',
  '末尾段落',
].join('\n');

function createKbNotesBackend() {
  const base = createToolFileBackend();
  let seq = 1;
  const kbNotes = new Map();
  const sessionNotes = [
    {
      noteUuid: 'sticky-1',
      scope: 'session',
      conversationUuid: uuid,
      conversationTitle: 'RouterOS 组网排障',
      conversationExists: true,
      sheetUuid: '',
      turnUuid: '',
      turnNumber: 2,
      opId: 'assistant:turn-2:0',
      quote: '将 WG 接口 MTU 固定为 1420 后大包不再分片丢包。',
      content: '回家后同步检查 10.10.10.254 配置',
      createdAtMs: 1700000000000,
      updatedAtMs: 1700000000000,
    },
  ];
  const globalSheets = [
    { sheetUuid: 'sheet-default', title: '默认便签', sortOrder: 0, createdAtMs: 1000, updatedAtMs: 1000, notes: [] },
  ];

  function addRawNote(data) {
    const id = data.noteUuid || `kb-${seq++}`;
    const item = {
      noteUuid: id,
      parentUuid: data.parentUuid || '',
      title: data.title || '未命名笔记',
      icon: data.icon || '',
      content: data.content || '',
      summary: String(data.content || '').replace(/[#>*`|[\]()]/g, ' ').trim().slice(0, 120),
      sortOrder: data.sortOrder ?? kbNotes.size,
      pinned: Boolean(data.pinned),
      isReadonly: Boolean(data.isReadonly),
      revision: 1,
      wordCount: String(data.content || '').length,
      createdAtMs: Date.now(),
      updatedAtMs: Date.now(),
    };
    kbNotes.set(id, item);
    return item;
  }

  // Seed a composite parent note + child note with table & code block
  const rootRec = addRawNote({
    noteUuid: 'kb-root-record',
    title: '记录',
    icon: '📚',
    content: '## 运维总览\n\n这里是父级笔记本自身的正文说明。',
    sortOrder: 0,
  });
  addRawNote({
    noteUuid: 'kb-wwan',
    parentUuid: rootRec.noteUuid,
    title: 'WWAN模块知识',
    icon: '📡',
    content: [
      '## 常用频段表',
      '',
      '| Band | 模式 | 备注 |',
      '| --- | --- | --- |',
      '| B1 | FDD | [主力频段]^(下行 2110-2170MHz\\n上行 1920-1980MHz) |',
      '| B78 | TDD | 5G 常用 |',
      '',
      '## 调试指令',
      '',
      '```bash',
      'AT+QENG="servingcell"',
      '```',
      '',
      '- [x] 实测信号驻网',
      '- [ ] 补测载波聚合',
    ].join('\n'),
    sortOrder: 0,
  });

  addRawNote({
    noteUuid: 'kb-fidelity',
    title: '转义与公式',
    content: FIDELITY_SOURCE,
    sortOrder: 1,
  });

  function computeTreeItems() {
    const all = [...kbNotes.values()];
    const childCountOf = (id) => all.filter((x) => x.parentUuid === id).length;
    const ancestorsOf = (item) => {
      const chain = [];
      let cur = item.parentUuid ? kbNotes.get(item.parentUuid) : null;
      const seen = new Set();
      while (cur && !seen.has(cur.noteUuid)) {
        seen.add(cur.noteUuid);
        chain.unshift({ noteUuid: cur.noteUuid, title: cur.title, icon: cur.icon });
        cur = cur.parentUuid ? kbNotes.get(cur.parentUuid) : null;
      }
      return chain;
    };
    return all
      .sort((a, b) => Number(b.pinned) - Number(a.pinned) || a.sortOrder - b.sortOrder)
      .map((item) => {
        const anc = ancestorsOf(item);
        const path = [...anc.map((x) => x.title), item.title].join(' / ');
        return {
          ...item,
          depth: anc.length,
          childCount: childCountOf(item.noteUuid),
          ancestors: anc,
          path,
        };
      });
  }

  function detailOf(id) {
    const rows = computeTreeItems();
    const found = rows.find((x) => x.noteUuid === id);
    if (!found) return null;
    return {
      ...found,
      children: rows.filter((x) => x.parentUuid === id),
    };
  }

  const backend = {
    ...base,
    kbNotes,
    patches: [],
    conflictOnce: '',
    handle(request) {
      if (request.path === '/api/settings' && request.method === 'GET') {
        const res = base.handle(request);
        return {
          ...res,
          json: {
            ...res.json,
            values: { ...(res.json?.values || {}), 'ui.notesEnabled': true, 'ui.notesSelectionPopover': true },
          },
        };
      }
      if (request.path === '/api/notes' && request.method === 'GET') {
        return { status: 200, json: { ok: true, sessionNotes, globalSheets } };
      }
      if (request.path === '/api/kb-notes/tree' && request.method === 'GET') {
        const items = computeTreeItems();
        return { status: 200, json: { ok: true, items, total: items.length } };
      }
      if (request.path === '/api/kb-notes/search' && request.method === 'GET') {
        const q = String(request.query?.q || '').toLowerCase();
        const items = computeTreeItems()
          .filter((x) => `${x.title} ${x.content}`.toLowerCase().includes(q))
          .map((x) => ({ ...x, snippet: x.summary }));
        return { status: 200, json: { ok: true, items } };
      }
      if (request.path === '/api/kb-notes/items' && request.method === 'POST') {
        const body = request.body || {};
        const created = addRawNote({
          parentUuid: body.parentUuid || '',
          title: body.title || '无标题笔记',
          icon: body.icon || '',
          content: body.content || '',
        });
        return { status: 200, json: { ok: true, note: detailOf(created.noteUuid) } };
      }
      const itemMatch = /^\/api\/kb-notes\/items\/([^/]+)$/.exec(request.path);
      if (itemMatch && request.method === 'GET') {
        const d = detailOf(decodeURIComponent(itemMatch[1]));
        return d ? { status: 200, json: { ok: true, note: d } } : { status: 404, json: { ok: false, error: 'not_found' } };
      }
      if (itemMatch && request.method === 'PATCH') {
        const id = decodeURIComponent(itemMatch[1]);
        const raw = kbNotes.get(id);
        if (!raw) return { status: 404, json: { ok: false, error: 'not_found' } };
        const b = request.body || {};
        backend.patches.push({ id, body: b });
        if (backend.conflictOnce === id) {
          backend.conflictOnce = '';
          raw.content = '服务器上的新版本';
          raw.revision += 1;
          return { status: 409, json: { ok: false, error: 'kb_note_revision_conflict', message: '笔记已在其他位置更新', currentNote: detailOf(id) } };
        }
        if (b.title !== undefined) raw.title = b.title;
        if (b.icon !== undefined) raw.icon = b.icon;
        if (b.content !== undefined) {
          raw.content = b.content;
          raw.summary = String(b.content).slice(0, 120);
        }
        if (b.pinned !== undefined) raw.pinned = Boolean(b.pinned);
        if (b.isReadonly !== undefined) raw.isReadonly = Boolean(b.isReadonly);
        raw.revision += 1;
        raw.updatedAtMs = Date.now();
        return { status: 200, json: { ok: true, note: detailOf(id) } };
      }
      if (request.path === '/api/kb-notes/capture' && request.method === 'POST') {
        const b = request.body || {};
        const refLine = b.opId
          ? `来源：[${b.conversationTitle || '原会话'} · 第 ${b.turnNumber || 1} 轮](openbear://ref/message/${encodeURIComponent(b.conversationUuid)}/${encodeURIComponent(b.opId)}?mode=mention)`
          : '';
        const quoteBlock = b.quote ? b.quote.split('\n').map((l) => `> ${l}`).join('\n') : '';
        const block = [refLine, quoteBlock, b.content || ''].filter(Boolean).join('\n\n');
        if (b.mode === 'append' && b.targetNoteUuid && kbNotes.has(b.targetNoteUuid)) {
          const target = kbNotes.get(b.targetNoteUuid);
          target.content = `${target.content}\n\n---\n\n${block}`.trim();
          target.revision += 1;
          return { status: 200, json: { ok: true, note: detailOf(target.noteUuid) } };
        }
        const created = addRawNote({
          parentUuid: b.parentUuid || '',
          title: b.title || '会话摘录',
          icon: '📝',
          content: block,
        });
        return { status: 200, json: { ok: true, note: detailOf(created.noteUuid) } };
      }
      if (request.path === '/api/kb-notes/import/files' && request.method === 'POST') {
        const doc = addRawNote({ title: 'Clash分流笔记', icon: '🌐', content: '## 规则策略\n\n已从 Markdown 导入。' });
        return { status: 200, json: { ok: true, createdCount: 1, importedAssets: 0, firstNoteUuid: doc.noteUuid } };
      }
      return base.handle(request);
    },
  };
  return backend;
}

test('global tree notes workspace: WYSIWYG table/code/source toggle, lossless edits, conflicts, Markdown import, sticky-note capture & mobile responsive layout', { skip: !chromium, timeout: 180000 }, async (t) => {
  const fixture = await startBrowserTestServer(t, {
    name: 'kb-notes-workspace',
    entries: ['virtual:kb-notes-test', 'src/views/NotesView.vue', 'src/components/MdEditor.vue'],
    plugins: [{
      name: 'kb-notes-test',
      resolveId(id) {
        if (id === 'virtual:kb-notes-test') return '\0virtual:kb-notes-test';
      },
      load(id) {
        if (id === '\0virtual:kb-notes-test') return entry;
      },
      configureServer(server) {
        server.middlewares.use((req, res, next) => {
          if (req.url?.startsWith('/notes') || req.url?.startsWith('/chat')) {
            res.setHeader('content-type', 'text/html');
            res.end(html);
          } else next();
        });
      },
    }],
  });

  let browser;
  t.after(async () => {
    try { await browser?.close(); } finally { await fixture.close(); }
  });
  browser = await chromium.launch({
    executablePath: process.env.OPENBEAR_CHROME || '/usr/bin/google-chrome',
    headless: true,
    args: ['--no-sandbox'],
  });

  await t.test('desktop 1440px: composite tree, WYSIWYG table + code block + Monaco source toggle, lossless edit, conflict copy, Markdown import & capture from chat sticky note', { timeout: 90000 }, async (t) => {
    const context = await browser.newContext({ viewport: { width: 1440, height: 880 } });
    t.after(() => context.close());
    const page = await context.newPage();
    const errors = [];
    const backend = createKbNotesBackend();
    page.on('pageerror', (err) => { console.error('[pageerror]', err); errors.push(err.message); });
    await page.addInitScript(() => {
      localStorage.setItem('openbear.notifications.prompt.v1', 'dismissed');
    });
    await page.route('**/api/**', async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      let parsedBody = null;
      try { parsedBody = req.postDataJSON(); } catch {}
      const { status, json, headers, body } = backend.handle({
        method: req.method(),
        path: url.pathname,
        query: Object.fromEntries(url.searchParams),
        body: parsedBody,
      });
      if (json !== undefined) return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(json) });
      return route.fulfill({ status, headers, body });
    });
    await page.routeWebSocket('**/api/**', backend.socket);

    // 1. Start on /chat, open sticky note, capture to global KB note & jump straight to /notes
    await page.goto(`${fixture.base}/chat?id=${uuid}`);
    await page.locator('.notes-entry').waitFor({ state: 'visible', timeout: 30000 });
    await page.locator('.notes-entry').click();
    const stickyPopover = page.locator('.notes-sticky-popover');
    await stickyPopover.waitFor({ state: 'visible' });
    await stickyPopover.getByRole('button', { name: '存入全局笔记' }).click();

    const capDialog = page.locator('.kb-capture-dialog');
    await capDialog.waitFor({ state: 'visible' });
    await capDialog.getByRole('button', { name: '保存并打开笔记' }).click();

    // Should navigate to /notes and render the captured note with reference capsule & blockquote
    await page.waitForFunction(() => window.location.pathname === '/notes');
    await page.locator('.kb-notes-page').waitFor({ state: 'visible' });
    await page.locator('.sy-protyle-content .reference-chip').first().waitFor({ state: 'visible' });
    assert.match(await page.locator('.sy-protyle-content blockquote').innerText(), /MTU 固定为 1420/);

    // 2. Expand composite parent note「记录」-> verify parent has its own body AND child「WWAN模块知识」
    const recordRow = page.locator('.kb-tree-row').filter({ hasText: '记录' }).first();
    await recordRow.locator('.kb-row-main').click();
    assert.equal(await page.locator('.sy-doc-title-input').inputValue(), '记录');
    assert.match(await page.locator('.sy-protyle-content').innerText(), /父级笔记本自身的正文说明/);

    const expandChevron = recordRow.locator('.kb-row-chevron');
    if ((await expandChevron.getAttribute('aria-label')) === '展开子笔记') {
      await expandChevron.click();
    }
    const wwanRow = page.locator('.kb-tree-row').filter({ hasText: 'WWAN模块知识' });
    await wwanRow.waitFor({ state: 'visible' });
    await wwanRow.locator('.kb-row-main').click();

    // 3. Verify WYSIWYG table, contextual table bar, code block language selector, task list, and TOC
    await page.locator('.sy-protyle-content table').waitFor({ state: 'visible' });
    await page.locator('.sy-protyle-content table td').first().click();
    const tableBar = page.locator('.sy-table-bar');
    await tableBar.waitFor({ state: 'visible' });
    await tableBar.getByRole('button', { name: '+下行' }).click();
    assert.equal(await page.locator('.sy-protyle-content table tr').count(), 4);

    assert.equal(await page.locator('.sy-code-lang-select').first().inputValue(), 'bash');
    assert.equal(await page.locator('.sy-task-item').count(), 2);

    // 3b. Verify SiYuan-style left block gutter: hover codeBlock -> click gutter -> whole block selected -> Backspace deletes it -> Ctrl+Z restores it; Right-click block -> selects block & opens context menu
    const codeBlock = page.locator('.sy-code-block').first();
    await codeBlock.hover();
    const gutterBtn = page.locator('.sy-block-gutter-btn').last();
    await gutterBtn.waitFor({ state: 'visible' });
    await gutterBtn.click();
    await page.locator('.sy-code-block.sy-block-selected').waitFor({ state: 'visible' });
    await page.keyboard.press('Backspace');
    assert.equal(await page.locator('.sy-code-block').count(), 0);
    await page.keyboard.press('Control+z');
    await page.locator('.sy-code-block').first().waitFor({ state: 'visible' });

    // Right-click on plain table cell -> block-selects table & opens block context menu
    await page.locator('.sy-protyle-content table td').first().click({ button: 'right' });
    await page.locator('.sy-block-menu').waitFor({ state: 'visible' });
    await page.keyboard.press('Escape');
    await page.locator('.sy-block-menu').waitFor({ state: 'hidden' });

    // 3c. Verify Top Formatting Toolbar & SiYuan Inline Memo right-click popover
    await page.getByRole('toolbar', { name: '编辑格式工具栏' }).waitFor({ state: 'visible' });
    await page.getByRole('toolbar', { name: '编辑格式工具栏' }).getByRole('button', { name: '备注' }).waitFor({ state: 'visible' });

    const memoSpan = page.locator('.sy-protyle-content span.sy-inline-memo', { hasText: '主力频段' });
    await memoSpan.waitFor({ state: 'visible' });
    await memoSpan.click({ button: 'right' });
    const memoPopover = page.locator('.sy-memo-popover');
    await memoPopover.waitFor({ state: 'visible' });
    assert.equal(await page.locator('.sy-block-menu').isVisible(), false, 'right-clicking inline-memo should open memo popover, not block menu');
    const memoArea = memoPopover.getByRole('textbox', { name: '备注正文' });
    assert.match(await memoArea.inputValue(), /2110-2170MHz/);
    await memoArea.fill('下行 2110MHz（已校准）');
    await memoPopover.getByRole('button', { name: '关闭备注' }).click();
    await memoPopover.waitFor({ state: 'hidden' });

    // 4. Switch to Monaco source mode and back to WYSIWYG without losing table structure
    await page.getByRole('tab', { name: '源码' }).click();
    await page.locator('.sy-source-stage').waitFor({ state: 'visible', timeout: 15000 });
    await page.getByRole('tab', { name: '所见即所得' }).click();
    assert.equal(await page.locator('.sy-protyle-content table tr').count(), 4);

    // 4b. Typing in a note with escapes, nesting, soft breaks, math and front matter only adds the typed text
    await page.locator('.kb-tree-row').filter({ hasText: '转义与公式' }).locator('.kb-row-main').click();
    await page.locator('.sy-protyle-content .sy-raw-block').first().waitFor({ state: 'visible' });
    assert.match(await page.locator('.sy-protyle-content').innerText(), /字面 \*星号\* 与 粗 斜 粗[\s\S]*# 不是标题/);
    assert.equal(await page.locator('.sy-protyle-content h1').count(), 1);
    const lastParagraph = page.locator('.sy-protyle-content p', { hasText: '末尾段落' });
    await lastParagraph.click();
    await page.keyboard.press('End');
    const fidelityPatch = page.waitForRequest((req) => req.method() === 'PATCH' && req.url().includes('/kb-notes/items/kb-fidelity'));
    await page.keyboard.type('追加');
    const patchBody = (await fidelityPatch).postDataJSON();
    assert.equal(patchBody.content, `${FIDELITY_SOURCE}追加`);

    // 4c. A revision conflict asks the user; loading the latest keeps local text as a sibling copy
    // The caret is still at the end of that paragraph.
    backend.conflictOnce = 'kb-fidelity';
    await page.keyboard.type('！');
    const conflictBox = page.locator('.el-message-box').filter({ hasText: '笔记版本冲突' });
    await conflictBox.waitFor({ state: 'visible' });
    await conflictBox.getByRole('button', { name: '载入最新版本' }).click();
    await page.locator('.kb-tree-row').filter({ hasText: '转义与公式（冲突副本）' }).waitFor({ state: 'visible' });
    await page.locator('.sy-protyle-content', { hasText: '服务器上的新版本' }).waitFor({ state: 'visible' });
    const conflictCopy = [...backend.kbNotes.values()].find((n) => n.title === '转义与公式（冲突副本）');
    assert.equal(conflictCopy?.content, `${FIDELITY_SOURCE}追加！`);

    // 5. Markdown / ZIP import (no SiYuan entry any more)
    await page.getByRole('button', { name: '导入笔记' }).click();
    const importDlg = page.locator('.kb-import-dialog');
    await importDlg.waitFor({ state: 'visible' });
    assert.doesNotMatch(await importDlg.innerText(), /思源/);
    await importDlg.locator('input[type=file]').setInputFiles({ name: 'Clash分流笔记.md', mimeType: 'text/markdown', buffer: Buffer.from('# Clash分流笔记\n\n规则') });
    await importDlg.getByRole('button', { name: '导入所选文件' }).click();
    await page.locator('.kb-tree-row').filter({ hasText: 'Clash分流笔记' }).waitFor({ state: 'visible' });

    // 6. Long title wraps onto multiple lines, keeps topbar actions inside viewport, and left smile icon stays clickable across hover gap
    await page.locator('.kb-tree-row').filter({ hasText: 'Clash分流笔记' }).locator('.kb-row-main').click();
    const titleBox = page.locator('textarea.sy-doc-title-input');
    await titleBox.fill('连做4条百万曝光产品宣传片后，我想给独立开发者留下一份真正能用的教程，跳出Skill，和AI聊出适合自己产品的片子');
    const titleHeight = await titleBox.evaluate((el) => el.offsetHeight);
    assert.ok(titleHeight >= 80, `long title should auto-wrap onto >=2 lines, got height ${titleHeight}`);

    const exportBox = await page.getByRole('button', { name: '导出 Markdown' }).boundingBox();
    assert.ok(exportBox && exportBox.x + exportBox.width <= 1440, `export button pushed off-screen: ${JSON.stringify(exportBox)}`);

    // Clear custom icon first so the note is in iconless state (.sy-doc-gutter-icon), then verify hover bridge from title to left smile icon
    const heroBtn = page.getByRole('button', { name: '更换笔记图标' });
    if (await heroBtn.isVisible()) {
      await heroBtn.click();
      await page.getByRole('button', { name: '清除自定义图标' }).click();
    }
    await page.locator('.sy-protyle-content').click();
    await titleBox.hover();
    const smileBtn = page.getByRole('button', { name: '设置笔记图标' });
    await smileBtn.hover();
    await smileBtn.click();
    await page.locator('.sy-emoji-grid').waitFor({ state: 'visible' });
    await page.keyboard.press('Escape');

    assert.deepEqual(errors, []);
  });

  await t.test('mobile 390px & narrow 320px: compact tree list, mobile editor view, outline drawer, dark theme & zero horizontal overflow', { timeout: 60000 }, async (t) => {
    const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true });
    t.after(() => context.close());
    const page = await context.newPage();
    const errors = [];
    const backend = createKbNotesBackend();
    page.on('pageerror', (err) => { console.error('[pageerror]', err); errors.push(err.message); });
    await page.addInitScript(() => {
      localStorage.setItem('openbear.notifications.prompt.v1', 'dismissed');
      localStorage.setItem('openbear.theme.mode', 'dark');
    });
    await page.route('**/api/**', async (route) => {
      const req = route.request();
      const url = new URL(req.url());
      let parsedBody = null;
      try { parsedBody = req.postDataJSON(); } catch {}
      const { status, json, headers, body } = backend.handle({
        method: req.method(),
        path: url.pathname,
        query: Object.fromEntries(url.searchParams),
        body: parsedBody,
      });
      if (json !== undefined) return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(json) });
      return route.fulfill({ status, headers, body });
    });
    await page.routeWebSocket('**/api/**', backend.socket);

    await page.goto(`${fixture.base}/notes`);
    await page.locator('.kb-notes-page').waitFor({ state: 'visible', timeout: 30000 });

    // Mobile starts in compact tree list view (not split side-by-side)
    assert.equal(await page.locator('.kb-tree-pane').isVisible(), true);
    const filterFontSize = await page.locator('.kb-tree-filter input').evaluate((el) => parseFloat(getComputedStyle(el).fontSize));
    assert.ok(filterFontSize >= 16, `mobile filter input should be >=16px to prevent iOS zoom, got ${filterFontSize}`);

    // Expand「记录」and tap「WWAN模块知识」to enter mobile editor view
    const recRow = page.locator('.kb-tree-row').filter({ hasText: '记录' }).first();
    const chevron = recRow.locator('.kb-row-chevron');
    if ((await chevron.getAttribute('aria-label')) === '展开子笔记') {
      await chevron.click();
    }
    await page.locator('.kb-tree-row').filter({ hasText: 'WWAN模块知识' }).locator('.kb-row-main').click();

    await page.getByRole('button', { name: '返回笔记列表' }).waitFor({ state: 'visible' });
    assert.equal(await page.locator('.kb-tree-pane').isVisible(), false);

    // Open mobile bottom outline sheet
    await page.getByRole('button', { name: '打开文档大纲' }).click();
    await page.locator('.kb-mobile-outline-sheet').waitFor({ state: 'visible' });
    await page.locator('.kb-mobile-outline-sheet .sy-outline-item').first().click();
    await page.locator('.kb-mobile-outline-sheet').waitFor({ state: 'hidden' });

    // Narrow 320px check: no page-level horizontal overflow
    await page.setViewportSize({ width: 320, height: 640 });
    const overflow320 = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert.ok(overflow320 <= 1, `expected no horizontal overflow on 320px, got ${overflow320}`);

    // Tap back to return to tree list
    await page.getByRole('button', { name: '返回笔记列表' }).click();
    assert.equal(await page.locator('.kb-tree-pane').isVisible(), true);

    assert.deepEqual(errors, []);
  });
});
