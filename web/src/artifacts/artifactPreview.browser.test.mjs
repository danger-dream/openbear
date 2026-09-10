import assert from "node:assert/strict";
import test from "node:test";
import {createServer} from "node:http";
import {mkdtemp, mkdir, readFile, rm} from "node:fs/promises";
import {tmpdir} from "node:os";
import path from "node:path";
import postcss from "postcss";
import tailwindcss from "tailwindcss";
import {chromium} from "playwright-core";
import {browserExecutable, componentBundle, webRoot, testPng} from "../../test-support/realBrowser.mjs";

const conversation = "39a541d4-4d9c-4a58-87d6-1b276779954a";
const id = n => `adfead18-e6d1-40df-8475-${String(n).padStart(12, "0")}`;
const contentUrl = n => `/api/conversations/${conversation}/artifacts/${id(n)}/content`;
const link = (n, label = "查看附件") => `[${label}](${contentUrl(n)})`;
const documentText = "# 模型管理交互优化方案\n\n统一账户与渠道模型的管理入口，让配置职责清晰，阅读与讨论留在同一段对话中。\n\n## 核心结构\n\n| 页面 | 职责 |\n| --- | --- |\n| 账户与渠道 | 管理可用模型 |\n| 能力与价格 | 查看有效元数据 |\n\n```js\nconst model = 'example';\n```\n\n" + Array.from({length: 45}, (_, i) => `## ${i + 1}. 页面与返回路径\n\n这里是方案正文。**中文强调。**后续文字继续保持正常排版。`).join("\n\n");
const htmlText = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>隔离交互测试</title>
<style>body{margin:0;font:14px sans-serif;background:#fff;color:#123456}button{padding:8px}#tail{margin-top:1600px}</style></head>
<body><h1>原始 HTML</h1><button id="counter" onclick="this.textContent = String(Number(this.textContent) + 1)">0</button>
<div id="tail">页面末尾</div><img src="/probe/image"><script src="/probe/script"></script><iframe src="/probe/frame"></iframe>
<form action="/probe/form" method="post"><input name="test" value="fixture-only"><button id="submit">提交测试</button></form>
<script>
window.__unsafeExecuted=true;
window.guard={};
try { parent.document.body.dataset.unsafe='yes'; } catch { guard.parent=true; }
try { localStorage.setItem('fixture-only','test'); } catch { guard.storage=true; }
try { document.cookie='fixture-only=test'; } catch { guard.cookie=true; }
fetch('/probe/api', {method:'POST',credentials:'include',body:'fixture-only'}).catch(() => {guard.fetch=true;});
window.addEventListener('securitypolicyviolation', e => {if(e.violatedDirective === 'form-action') guard.form=true;});
</script></body></html>`;
const executablePath = browserExecutable();

test("attachment cards and in-conversation reader", {skip: !executablePath && "Set CHROME_BIN to Chromium", timeout: 90000}, async t => {
	const bundle = await componentBundle(`
		import {createApp, h, reactive, nextTick} from 'vue';
		import ElementPlus from 'element-plus';
		import ConsoleMarkdown from './src/views/consoleView/ConsoleMarkdown.vue';
		import ArtifactPreview from './src/artifacts/ArtifactPreview.vue';
		import {artifactFromUrl, openArtifactPreview, clearArtifactCache} from './src/artifacts/artifactFiles.js';
		const state = reactive({text: '', live: false, nav: 'main'});
		createApp({render: () => [h('section', {class: 'test-conversation'}, [h('div', {class: 'test-conversation-eyebrow'}, 'OPENBEAR / 对话'), h('h1', '模型管理交互方案'), h('p', {class: 'test-conversation-intro'}, '梳理完了，完整方案与页面预览已整理为附件。'), h(ConsoleMarkdown, {text: state.text, live: state.live})]), h(ArtifactPreview, {navigationKey: state.nav})]}).use(ElementPlus).mount('#app');
		window.fixture = {async set(text, live = false) {state.text = text; state.live = live; await nextTick();}, async navigate(nav) {state.nav = nav; await nextTick();}, open(href, label = '') {openArtifactPreview(artifactFromUrl(href), label, document.activeElement);}, clearArtifactCache};
	`);
	const baseCss = await readFile(path.join(webRoot, "node_modules/element-plus/dist/index.css"), "utf8");
	const appStylePath = path.join(webRoot, "src/style.css");
	const appCss = (await postcss([tailwindcss({content: [{raw: '<div class="flex min-h-0"></div>', extension: 'html'}]})]).process(await readFile(appStylePath, "utf8"), {from: appStylePath})).css;
	const epDarkCss = await readFile(path.join(webRoot, "node_modules/element-plus/theme-chalk/dark/css-vars.css"), "utf8");
	const darkCss = await readFile(path.join(webRoot, "src/dark-theme.css"), "utf8");
	const highlightCss = await readFile(path.join(webRoot, "node_modules/highlight.js/styles/github.css"), "utf8");
	const png = testPng(1800, 1200);
	const files = new Map([
		[1, {name: "模型管理交互优化方案.md", mime: "text/markdown", text: documentText}],
		[2, {name: "settings.json", mime: "application/json", text: '{"model":"example","enabled":true}'}],
		[3, {name: "preview.png", mime: "image/png", bytes: png}],
		[4, {name: "page.html", mime: "text/html", text: htmlText}],
		[5, {name: "drawing.svg", mime: "image/svg+xml", text: '<svg xmlns="http://www.w3.org/2000/svg" onload="window.__unsafeExecuted=true"><text>test</text></svg>'}],
		[6, {name: "files.zip", mime: "application/zip", bytes: Buffer.from([0, 1, 2])}],
		[7, {name: "large.md", mime: "text/markdown", text: "# Large", size: 3 * 1024 * 1024}],
		[8, {name: "delayed.md", mime: "text/markdown", text: "# 迟到的文档\n\n不能覆盖新的预览。", size: 100 * 1024}],
		[9, {name: "missing.md", mime: "text/markdown", text: "# 已恢复的附件"}],
		[10, {name: "empty.txt", mime: "text/plain", text: ""}],
		[11, {name: "below-fold.md", mime: "text/markdown", text: "# 下方附件\n\n进入可视区后读取。"}],
	]);
	// Optional local acceptance uses the user's actual demo unchanged; CI uses the committed fixture above.
	if (process.env.ARTIFACT_HTML_FIXTURE) files.set(12, {name: "model-center-simple.html", mime: "text/html", text: await readFile(process.env.ARTIFACT_HTML_FIXTURE, "utf8")});
	const counts = new Map(), missing = new Set([9]), held = new Set([8]), pending = [];
	const server = createServer((req, res) => {
		counts.set(req.url, (counts.get(req.url) || 0) + 1);
		if (req.url === "/bundle.js") { res.writeHead(200, {"Content-Type": "text/javascript"}); return res.end(bundle); }
		if (req.url === "/style.css") { res.writeHead(200, {"Content-Type": "text/css"}); return res.end(baseCss + "\n" + epDarkCss + "\n" + appCss + "\n" + highlightCss + "\n" + darkCss + '\n*{box-sizing:border-box}body{margin:0;background:var(--ob-bg,#f5f6f8);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC","Microsoft YaHei",sans-serif;color:var(--ob-text,#303641)}button,input{font:inherit}.test-conversation{width:min(790px,100%);margin:0 auto;padding:52px 30px 140px}.test-conversation-eyebrow{color:var(--ob-text-muted,#98a0ac);font-size:10px;letter-spacing:.14em}.test-conversation>h1{margin:16px 0;font-size:24px;font-weight:650}.test-conversation-intro{font-size:13px;color:var(--ob-text-subtle,#818b9a);line-height:1.8;margin:0 0 24px}.test-conversation>.bear-md{font-size:14px;line-height:1.8}@media(max-width:760px){.test-conversation{padding:28px 18px 100px}.test-conversation>h1{font-size:20px}}'); }
		const match = req.url.match(/\/artifacts\/adfead18-e6d1-40df-8475-(\d{12})(\/content)?(?:\?(.*))?$/);
		if (match) {
			const n = Number(match[1]), file = files.get(n);
			if (!file || missing.has(n)) { res.writeHead(404); return res.end("missing"); }
			const bytes = file.bytes || Buffer.from(file.text);
			if (!match[2]) { res.writeHead(200, {"Content-Type": "application/json"}); return res.end(JSON.stringify({ok: true, artifact: {artifactUuid: id(n), conversationUuid: conversation, fileName: file.name, mimeType: file.mime, sizeBytes: file.size ?? bytes.length, sha256: `file-${n}`, contentUrl: contentUrl(n), downloadUrl: contentUrl(n) + "?download=1"}})); }
			const send = () => { res.writeHead(200, {"Content-Type": file.mime, "Content-Length": bytes.length, "Content-Disposition": `${match[3]?.includes("download=1") || file.mime === "text/html" ? "attachment" : "inline"}; filename*=UTF-8''${encodeURIComponent(file.name)}`}); res.end(bytes); };
			if (held.has(n) && !match[3]) pending.push({n, send, res}); else send();
			return;
		}
		res.writeHead(200, {"Content-Type": "text/html"}); res.end('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/style.css"></head><body><div id="app"></div><script src="/bundle.js"></script></body></html>');
	});
	await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
	let browser, temp;
	t.after(async () => { await browser?.close(); for (const entry of pending) entry.res.destroy(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); if (temp) await rm(temp, {recursive: true, force: true}); });
	temp = await mkdtemp(path.join(tmpdir(), "openbear-artifact-preview-"));
	const screenshots = process.env.ARTIFACT_SCREENSHOT_DIR || path.join(temp, "screenshots"); await mkdir(screenshots, {recursive: true});
	browser = await chromium.launch({executablePath, headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"]});
	const context = await browser.newContext({viewport: {width: 1360, height: 960}, acceptDownloads: true});
	const page = await context.newPage();
	page.setDefaultTimeout(8000);
	const pageErrors = []; page.on("pageerror", error => pageErrors.push(error.message));
	const runtimeErrors = [];
	page.on('console', message => { if (message.type() === 'error' && /^(TypeError|ReferenceError|SyntaxError|Error):/.test(message.text())) runtimeErrors.push(message.text()); });
	await page.goto(`http://127.0.0.1:${server.address().port}/`, {waitUntil: "domcontentloaded"});
	await page.waitForFunction(() => window.fixture);
	const set = (text, live = false) => page.evaluate(({text, live}) => window.fixture.set(text, live), {text, live});
	const open = n => page.evaluate(href => window.fixture.open(href), contentUrl(n));
	const close = async () => { await page.getByRole("button", {name: "关闭附件预览", exact: true}).click(); await page.waitForFunction(() => !document.querySelector('.artifact-preview-dialog') || getComputedStyle(document.querySelector('.artifact-preview-dialog')).display === 'none'); };
	const contentCount = n => counts.get(contentUrl(n)) || 0;
	const metadataCount = n => counts.get(contentUrl(n).slice(0, -8)) || 0;

	await t.test("the historical emoji/bold file link becomes a card with actual title and excerpt; no offscreen eager downloads", async () => {
		await set(`📄 **${link(1, "查看完整 Markdown 方案与页面预览")}**\n\n` + "阅读中的对话段落。\n\n".repeat(65) + link(11));
		await page.waitForFunction(() => document.querySelector('.artifact-card-title')?.textContent === '模型管理交互优化方案');
		assert.equal(await page.locator(".artifact-card").count(), 2);
		assert.match(await page.locator(".artifact-card-excerpt").first().textContent(), /统一账户与渠道模型/);
		assert.equal(metadataCount(1), 1); assert.equal(contentCount(1), 1);
		assert.equal(metadataCount(11), 0); assert.equal(contentCount(11), 0);
		await page.screenshot({path: path.join(screenshots, "attachment-card-desktop.png")});
	});

	await t.test("opening renders Markdown in a modal, supports source/download, and restores document/chat position and focus", async () => {
		const card = page.locator(".artifact-card-open").first(); await card.focus();
		const position = await page.evaluate(() => window.scrollY);
		await card.click(); await page.waitForSelector(".artifact-preview-document h1");
		assert.equal(await page.locator(".artifact-preview-document h1").first().textContent(), "模型管理交互优化方案");
		assert.equal(await page.locator(".artifact-preview-document table").count(), 1);
		assert.ok(await page.locator(".artifact-preview-document .hljs-keyword").count());
		assert.equal(context.pages().length, 1); assert.equal(contentCount(1), 1);
		await page.screenshot({path: path.join(screenshots, "attachment-reader-desktop.png")});
		await page.locator(".artifact-preview-body").evaluate(el => { el.scrollTop = 400; el.dispatchEvent(new Event('scroll')); });
		await page.getByRole("button", {name: "源码", exact: true}).click();
		assert.match(await page.locator(".artifact-preview-source code").textContent(), /^# 模型管理/);
		await page.locator(".artifact-preview-body").evaluate(el => { el.scrollTop = 160; el.dispatchEvent(new Event('scroll')); });
		await page.getByRole("button", {name: "文档预览", exact: true}).click();
		await page.waitForFunction(() => document.querySelector('.artifact-preview-body').scrollTop === 400);
		const downloadEvent = page.waitForEvent("download"); await page.getByRole("link", {name: "下载原文件", exact: true}).click();
		const download = await downloadEvent; assert.equal(download.suggestedFilename(), "模型管理交互优化方案.md");
		assert.equal(await readFile(await download.path(), "utf8"), documentText);
		assert.ok(await page.locator(".artifact-preview-dialog").isVisible());
		await close(); assert.equal(await page.evaluate(() => window.scrollY), position);
		assert.equal(await card.evaluate(el => document.activeElement === el), true);
		await card.click(); await page.waitForSelector(".artifact-preview-document");
		await page.waitForFunction(() => document.querySelector('.artifact-preview-body').scrollTop === 400);
		await page.keyboard.press("Escape"); await page.waitForFunction(() => !document.querySelector('.artifact-preview-document'));
	});

	await t.test("mixed paragraphs and list links become cards; surrounding text, tables, examples and images keep their meaning", async () => {
		await set(link(1));
		const downloadEvent = page.waitForEvent("download"); await page.locator(".artifact-card-download").click(); await downloadEvent;
		assert.equal(await page.locator(".artifact-preview-document").count(), 0);
		const original = `正文参见 ${link(2, "配置文件")}，继续讨论。\n\n- ${link(2)}\n\n| 文件 |\n| --- |\n| ${link(2)} |\n\n\`${link(2)}\`\n\n[外部网站](https://example.test/file.md)\n\n![图片](${contentUrl(3)})`;
		await set(original);
		assert.equal(await page.locator(".artifact-card").count(), 2);
		assert.equal(await page.locator(".test-conversation li .artifact-card").count(), 1); assert.equal(await page.locator(".test-conversation td a").count(), 1);
		assert.equal(await page.locator(".test-conversation .bear-md > p").first().textContent(), "正文参见 ");
		assert.equal(await page.locator(".test-conversation .bear-md > p").nth(1).textContent(), "，继续讨论。");
		assert.equal(await page.locator(".test-conversation code").textContent(), link(2));
		assert.equal(await page.locator(".test-conversation img").count(), 1);
		assert.equal(await page.getByRole("link", {name: "外部网站"}).getAttribute("target"), "_blank");
		await page.getByRole("button", {name: "预览附件：配置文件", exact: true}).click(); await page.waitForSelector(".artifact-preview-source .hljs-attr");
		assert.equal(await page.locator(".artifact-preview-source code").textContent(), files.get(2).text);
		assert.equal(context.pages().length, 1); await close();
	});

	await t.test("streaming keeps the mounted card, open dialog and reading position unchanged", async () => {
		await set(link(1) + "\n\n后续", true);
		await page.locator(".artifact-card-open").click(); await page.waitForSelector(".artifact-preview-document");
		const before = contentCount(1);
		const result = await page.evaluate(async href => {
			const card = document.querySelector('.artifact-card'), dialog = document.querySelector('.artifact-preview-dialog'), body = document.querySelector('.artifact-preview-body');
			body.scrollTop = 350; body.dispatchEvent(new Event('scroll'));
			for (let i = 1; i <= 20; i++) { await window.fixture.set(`[查看附件](${href})\n\n后续${'文字'.repeat(i)}`, true); await new Promise(resolve => setTimeout(resolve, 45)); }
			await window.fixture.set(`[查看附件](${href})\n\n后续${'文字'.repeat(20)}`, false);
			return {card: card === document.querySelector('.artifact-card'), dialog: dialog === document.querySelector('.artifact-preview-dialog'), body: body === document.querySelector('.artifact-preview-body'), top: body.scrollTop};
		}, contentUrl(1));
		assert.deepEqual(result, {card: true, dialog: true, body: true, top: 350}); assert.equal(contentCount(1), before); await close();
	});

	await t.test("heading/bold/multiple/nested links preserve text, markup, order and unique card identities", async () => {
		await set(`## 下载 **先看 ${link(4, 'HTML 演示')} 再看** 与 *${link(2, '配置')}* 完毕\n\n- 列表前 ${link(4, '列表演示')} 列表后\n  - 子级 ${link(2, '子级配置')} 后文\n\n> 引用前 **${link(4, '引用演示')}** 引用后\n\n[直接下载](${contentUrl(4)}?download=1)\n\n[![链接图片](${contentUrl(3)})](${contentUrl(3)})\n\n\`\`\`md\n${link(4)}\n\`\`\``);
		assert.equal(await page.locator('.artifact-card').count(), 5);
		assert.equal(await page.locator('h2 .md-artifact-slot, strong .md-artifact-slot, em .md-artifact-slot, p .md-artifact-slot').count(), 0);
		assert.equal(await page.locator('li > .md-artifact-slot').count(), 2);
		assert.equal(await page.locator('blockquote > .md-artifact-slot').count(), 1);
		const layout = await page.locator('.test-conversation > .bear-md').evaluate(root => {
			const keys = [...root.querySelectorAll('[data-artifact-slot]')].map(el => el.dataset.artifactSlot);
			const copy = root.cloneNode(true);
			for (const slot of copy.querySelectorAll('[data-artifact-slot]')) slot.replaceWith('[' + slot.dataset.artifactLabel + ']');
			return {keys, text: copy.textContent.replace(/\s+/g, ' '), bold: [...root.querySelectorAll('h2 strong')].map(el => el.textContent)};
		});
		assert.equal(new Set(layout.keys).size, 5);
		assert.match(layout.text, /下载 先看 \[HTML 演示\] 再看 与 \[配置\] 完毕/);
		assert.match(layout.text, /列表前 \[列表演示\] 列表后/);
		assert.match(layout.text, /子级 \[子级配置\] 后文/);
		assert.match(layout.text, /引用前 \[引用演示\] 引用后/);
		assert.deepEqual(layout.bold, ['先看 ', ' 再看']);
		assert.equal(await page.locator('.test-conversation a > img').count(), 1);
		assert.equal(contentCount(4), 0, 'HTML cards must not fetch or execute their content');
		const downloadEvent = page.waitForEvent('download');
		await page.getByRole('link', {name: '直接下载', exact: true}).click();
		assert.equal(await readFile(await (await downloadEvent).path(), 'utf8'), htmlText);
		assert.equal(await page.locator('.artifact-preview-html').count(), 0);
		await set(`前文 ${link(4, 'HTML 演示')} 后文`);
		const kept = await page.evaluate(async href => {
			const card = document.querySelector('.artifact-card');
			for (let i = 1; i <= 5; i++) await window.fixture.set(`前文 [HTML 演示](${href}) 后文${'继续'.repeat(i)}`);
			return card === document.querySelector('.artifact-card');
		}, contentUrl(4));
		assert.equal(kept, true);
	});

	await t.test("HTML direct download, manual iframe rendering, local interaction and isolation", async () => {
		await set(`打开 ${link(4, 'HTML 页面')} 可查看演示。`);
		const downloadEvent = page.waitForEvent('download');
		await page.locator('.artifact-card-download').click();
		const download = await downloadEvent;
		assert.equal(download.suggestedFilename(), 'page.html');
		assert.equal(await readFile(await download.path(), 'utf8'), htmlText);
		assert.equal(contentCount(4), 0);
		assert.equal(await page.locator('.artifact-preview-html').count(), 0);
		await page.locator('.artifact-card-open').click(); await page.waitForSelector('.artifact-preview-source');
		assert.equal(await page.locator('.artifact-preview-source code').textContent(), htmlText);
		assert.equal(await page.locator('.artifact-preview-html').count(), 0);
		await page.getByRole('button', {name: '页面预览', exact: true}).click();
		const iframe = page.locator('.artifact-preview-html');
		assert.equal(await iframe.getAttribute('sandbox'), 'allow-scripts');
		const frame = await (await iframe.elementHandle()).contentFrame();
		await frame.waitForFunction(() => window.__unsafeExecuted && window.guard.fetch);
		assert.equal(await frame.locator('h1').textContent(), '原始 HTML');
		assert.equal(await frame.locator('body').evaluate(el => getComputedStyle(el).color), 'rgb(18, 52, 86)');
		assert.deepEqual(await frame.evaluate(() => ({parent: guard.parent, storage: guard.storage, cookie: guard.cookie, fetch: guard.fetch})), {parent: true, storage: true, cookie: true, fetch: true});
		await frame.locator('#counter').click(); assert.equal(await frame.locator('#counter').textContent(), '1');
		const headerBefore = await page.locator('.artifact-preview-header').boundingBox();
		await frame.evaluate(() => window.scrollTo(0, 1000));
		assert.ok(await frame.evaluate(() => window.scrollY) > 0);
		assert.deepEqual(await page.locator('.artifact-preview-header').boundingBox(), headerBefore);
		await frame.locator('#submit').click();
		// Parent/top navigation and popups stay blocked by sandbox, including user-triggered forms.
		const protectedState = await frame.evaluate(() => {
			let topBlocked = false; try { top.location.href = '/probe/top'; } catch { topBlocked = true; }
			return {topBlocked, popupBlocked: window.open('/probe/popup') === null};
		});
		assert.deepEqual(protectedState, {topBlocked: true, popupBlocked: true});
		assert.equal(context.pages().length, 1);
		assert.equal(await page.evaluate(() => Boolean(window.__unsafeExecuted || document.body.dataset.unsafe)), false);
		assert.deepEqual([...counts.keys()].filter(key => key.startsWith('/probe/')), []);
		await page.screenshot({path: path.join(screenshots, 'html-preview-desktop.png')});
		await page.getByRole('button', {name: '源码', exact: true}).click();
		assert.equal(await page.locator('.artifact-preview-html').count(), 0);
		assert.equal(await page.locator('.artifact-preview-source code').textContent(), htmlText);
		await page.getByRole('button', {name: '页面预览', exact: true}).click();
		await iframe.waitFor(); await close(); assert.equal(await iframe.count(), 0);
		await open(4); await iframe.waitFor(); // remembers manual choice
		assert.equal(contentCount(4), 1);
		await open(2); await page.waitForSelector('.artifact-preview-source'); assert.equal(await iframe.count(), 0);
		await open(4); await iframe.waitFor();
		await page.evaluate(() => window.fixture.navigate('html-close')); await iframe.waitFor({state: 'detached'});
	});

	await t.test("HTML viewport and controls work in desktop/mobile light/dark, with the actual demo when supplied", async () => {
		for (const viewport of [{width: 1360, height: 960}, {width: 390, height: 844}, {width: 320, height: 430}]) for (const dark of [false, true]) {
			await page.setViewportSize(viewport); await page.evaluate(dark => document.documentElement.classList.toggle('dark', dark), dark);
			await open(4); await page.locator('.artifact-preview-html').waitFor();
			await page.evaluate(async () => { await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))); for (const a of document.getAnimations()) { try { a.finish(); } catch {} } });
			const box = await page.locator('.artifact-preview-html').boundingBox();
			assert.ok(box.width > 250 && box.height > 180 && box.x >= 0 && box.x + box.width <= viewport.width + 1 && box.y + box.height <= viewport.height, JSON.stringify({viewport, box}));
			for (const selector of ['.artifact-preview-header', '.artifact-preview-toolbar', '.artifact-preview-footer']) {
				const rect = await page.locator(selector).boundingBox(); assert.ok(rect.x >= 0 && rect.y >= 0 && rect.x + rect.width <= viewport.width + 1 && rect.y + rect.height <= viewport.height + 1, JSON.stringify({viewport, selector, rect}));
			}
			if (viewport.width === 320 && dark) await page.screenshot({path: path.join(screenshots, 'html-preview-mobile-short-dark.png')});
			await close();
		}
		if (files.has(12)) {
			await page.setViewportSize({width: 1360, height: 960}); await open(12);
			await page.getByRole('button', {name: '页面预览', exact: true}).click();
			const frame = await (await page.locator('.artifact-preview-html').elementHandle()).contentFrame();
			await frame.waitForFunction(() => document.querySelector('#keyboard button') && document.querySelector('#message')?.textContent.trim());
			const before = await frame.locator('#message').textContent();
			await frame.getByRole('button', {name: '🔎 查询', exact: true}).click();
			await frame.waitForFunction(before => document.querySelector('#message').textContent !== before, before);
			await page.screenshot({path: path.join(screenshots, 'actual-html-demo.png')});
			assert.equal(await page.evaluate(() => Boolean(window.P || window.App)), false);
			await close();
		}
		await page.setViewportSize({width: 1360, height: 960}); await page.evaluate(() => document.documentElement.classList.remove('dark'));
	});

	await t.test("SVG remains source-only; images zoom; unsupported/oversize/empty files have honest states", async () => {
		for (const n of [5]) { await open(n); await page.waitForSelector(".artifact-preview-source"); assert.match(await page.locator(".artifact-preview-source code").textContent(), /window.__unsafeExecuted/); assert.equal(await page.evaluate(() => Boolean(window.__unsafeExecuted)), false); assert.equal(await page.locator(".artifact-preview-body script, .artifact-preview-body iframe, .artifact-preview-body img").count(), 0); await close(); }
		await open(3); await page.waitForFunction(() => document.querySelector('.artifact-preview-image-stage img')?.naturalWidth === 1800);
		const fit = await page.locator('.artifact-preview-body').evaluate(el => { const img = el.querySelector('img').getBoundingClientRect(); return {width: img.width, height: img.height, availableWidth: el.clientWidth - 48, availableHeight: el.clientHeight - 48}; });
		assert.ok(fit.width <= fit.availableWidth + 1 && fit.height <= fit.availableHeight + 1, JSON.stringify(fit));
		await page.getByRole("button", {name: "放大图片", exact: true}).click(); assert.match(await page.locator(".artifact-preview-zoom").textContent(), /%/); await close();
		await open(6); await page.getByText("这个文件暂不支持站内预览").waitFor(); assert.equal(contentCount(6), 0); await close();
		await open(7); await page.getByText("文件超过 2 MB 的文本预览上限，请下载查看。").waitFor(); assert.equal(contentCount(7), 0); await close();
		await open(10); await page.getByText("这是一个空文件").waitFor(); await close();
	});

	await t.test("404 can be retried; delayed content cannot replace another document or reopen a closed/navigated reader", async () => {
		await open(9); await page.getByText("附件不存在、已删除，或当前无权访问。").waitFor();
		missing.delete(9); await page.getByRole("button", {name: "重新读取", exact: true}).click(); await page.getByRole("heading", {name: "已恢复的附件", exact: true}).first().waitFor(); await close();
		await open(8); await page.waitForFunction(() => document.querySelector('.artifact-preview-empty strong')?.textContent === '正在打开附件');
		await open(2); await page.waitForSelector(".artifact-preview-source .hljs-attr");
		held.delete(8); for (const entry of pending.splice(0)) entry.send();
		await page.waitForFunction(() => !document.querySelector('.artifact-preview-body')?.textContent.includes('迟到的文档'));
		assert.equal(await page.locator(".artifact-preview-source code").textContent(), files.get(2).text);
		await page.evaluate(() => window.fixture.navigate('other-conversation')); await page.waitForFunction(() => !document.querySelector('.artifact-preview-source'));
		assert.equal(context.pages().length, 1);
	});

	await t.test("desktop maximize, mobile full-screen, keyboard close and dark theme stay within the viewport", async () => {
		await set(link(1)); await open(1); await page.waitForSelector(".artifact-preview-document");
		await page.getByRole("button", {name: "最大化预览", exact: true}).click();
		await page.waitForFunction(() => document.querySelector('.artifact-preview-dialog').classList.contains('is-fullscreen'));
		assert.equal(Math.round((await page.locator('.artifact-preview-dialog').boundingBox()).width), 1360); await close();
		await page.setViewportSize({width: 390, height: 844}); await page.evaluate(() => document.documentElement.classList.add('dark'));
		await open(1); await page.waitForSelector(".artifact-preview-document");
		await page.waitForFunction(() => document.querySelector('.artifact-preview-dialog')?.getBoundingClientRect().y === 0);
		const metrics = await page.locator('.artifact-preview-dialog').evaluate(el => { const rect = el.getBoundingClientRect(); return {x: rect.x, y: rect.y, width: rect.width, height: rect.height, bg: getComputedStyle(el).backgroundColor, fullscreen: el.classList.contains('is-fullscreen')}; });
		assert.equal(metrics.fullscreen, true); assert.deepEqual([Math.round(metrics.x), Math.round(metrics.y), Math.round(metrics.width), Math.round(metrics.height)], [0, 0, 390, 844]);
		assert.notEqual(metrics.bg, 'rgb(255, 255, 255)');
		const closeRect = await page.getByRole('button', {name: '关闭附件预览', exact: true}).boundingBox(); assert.ok(closeRect.x >= 0 && closeRect.x + closeRect.width <= 390);
		await page.screenshot({path: path.join(screenshots, "attachment-reader-mobile-dark.png")});
		await page.keyboard.press('Escape'); await page.waitForFunction(() => !document.querySelector('.artifact-preview-document'));
		await page.screenshot({path: path.join(screenshots, "attachment-card-mobile-dark.png")});
	});
	await t.test("card vertical whitespace is balanced and stays stable from placeholder to summary in both themes and sizes", async () => {
		const measure = () => page.locator('.artifact-card-open').evaluate(el => {
			const button = el.getBoundingClientRect(), copy = el.querySelector('.artifact-card-copy').getBoundingClientRect();
			return {top: copy.top - button.top, bottom: button.bottom - copy.bottom, height: button.height, excerptHeight: el.querySelector('.artifact-card-excerpt').getBoundingClientRect().height};
		});
		for (const width of [1360, 390]) for (const dark of [false, true]) {
			await set('');
			await page.setViewportSize({width, height: 960});
			await page.evaluate(dark => { document.documentElement.classList.toggle('dark', dark); window.fixture.clearArtifactCache(); }, dark);
			let release;
			const gate = new Promise(resolve => { release = resolve; });
			const pattern = `**/artifacts/${id(1)}`;
			await page.route(pattern, async route => { await gate; await route.continue(); });
			let placeholder, loaded;
			try {
				await set(link(1)); await page.locator('.artifact-card-open').waitFor();
				placeholder = await measure();
				release();
				await page.waitForFunction(() => document.querySelector('.artifact-card-title')?.textContent === '模型管理交互优化方案');
				loaded = await measure();
			} finally { release(); await page.unroute(pattern); }
			t.diagnostic(`Card spacing ${width}px/${dark ? 'dark' : 'light'}: ${JSON.stringify({placeholder, loaded})}`);
			for (const layout of [placeholder, loaded]) {
				assert.ok(Math.abs(layout.top - 16) < .1 && Math.abs(layout.bottom - 16) < .1, JSON.stringify(layout));
				assert.equal(layout.excerptHeight, 37);
			}
			assert.equal(placeholder.height, loaded.height);
		}
	});
	assert.deepEqual(pageErrors, []); assert.deepEqual(runtimeErrors, []);
	t.diagnostic(`Screenshots: ${screenshots}`);
});
