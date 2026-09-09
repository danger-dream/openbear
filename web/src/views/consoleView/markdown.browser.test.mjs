import assert from "node:assert/strict";
import {mkdtemp, rm} from "node:fs/promises";
import {createServer} from "node:http";
import {tmpdir} from "node:os";
import path from "node:path";
import {fileURLToPath} from "node:url";
import {crc32, deflateSync} from "node:zlib";
import test from "node:test";
import {build} from "esbuild";
import {parse, compileScript, compileStyle} from "@vue/compiler-sfc";
import {readFile} from "node:fs/promises";
import {chromium} from "playwright-core";
import {browserExecutable} from '../../../test-support/realBrowser.mjs';

const webRoot = fileURLToPath(new URL("../../../", import.meta.url));

// A real, large PNG whose IHDR and initial scanlines can arrive before the rest.
function png(width, height) {
	function chunk(type, bytes) {
		const body = Buffer.concat([Buffer.from(type), bytes]);
		const head = Buffer.alloc(4); head.writeUInt32BE(bytes.length);
		const tail = Buffer.alloc(4); tail.writeUInt32BE(crc32(body));
		return Buffer.concat([head, body, tail]);
	}
	const header = Buffer.alloc(13);
	header.writeUInt32BE(width, 0); header.writeUInt32BE(height, 4); header[8] = 8; header[9] = 2;
	const pixels = Buffer.alloc((width * 3 + 1) * height, 140);
	for (let row = 0; row < height; row++) pixels[row * (width * 3 + 1)] = 0;
	return Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]), chunk("IHDR", header), chunk("IDAT", deflateSync(pixels, {level: 0})), chunk("IEND", Buffer.alloc(0))]);
}

async function browserBundle() {
	const entry = `
		import {createApp, h, reactive, nextTick} from 'vue';
		import ConsoleMarkdown from './src/views/consoleView/ConsoleMarkdown.vue';
		import {renderMarkdown} from './src/views/consoleView/markdown.js';
		import {patchMarkdownDom} from './src/views/consoleView/markdownDom.js';
		let app, state;
		window.mdTest = {
			renderMarkdown, patchMarkdownDom,
			async mount(text, options = {}) {
				app?.unmount();
				state = reactive({text, live: false, ...options});
				app = createApp({render: () => h(ConsoleMarkdown, state)});
				app.component('el-image-viewer', {
					props: ['urlList', 'initialIndex'], emits: ['close'],
					render() { return h('button', {'data-test-viewer': JSON.stringify(this.urlList), 'data-index': this.initialIndex, onClick: () => this.$emit('close')}, 'close'); }
				});
				app.mount(document.getElementById('app'));
				await nextTick();
			},
			async update(text, live = state.live) { state.text = text; state.live = live; await nextTick(); }
		};
	`;
	const result = await build({
		stdin: {contents: entry, resolveDir: webRoot}, bundle: true, write: false, format: "iife", platform: "browser",
		define: {"process.env.NODE_ENV": '"production"'}, loader: {".css": "empty"},
		plugins: [{name: "test-real-sfc", setup(builder) {
			builder.onLoad({filter: /\.vue$/}, async args => {
				const {descriptor} = parse(await readFile(args.path, "utf8"), {filename: args.path});
				const id = "data-v-markdown-test";
				const compiled = compileScript(descriptor, {id, genDefaultAs: "Component", inlineTemplate: true});
				const css = descriptor.styles.map(style => compileStyle({source: style.content, filename: args.path, id, scoped: style.scoped}).code).join("\n");
				return {contents: `${compiled.content}\nComponent.__scopeId = '${id}';\nconst style = document.createElement('style'); style.textContent = ${JSON.stringify(css)}; document.head.append(style);\nexport default Component;`, loader: "js", resolveDir: path.dirname(args.path)};
			});
		}}],
	});
	return result.outputFiles[0].contents;
}

const executablePath = browserExecutable();
test("actual Markdown component browser regressions", {skip: !executablePath && "Set CHROME_BIN to a Chromium executable", timeout: 60000}, async t => {
	const bundle = await browserBundle();
	const largeImage = png(1800, 1200), smallImage = png(2, 2);
	const pending = new Set();
	let slowRequests = 0;
	const server = createServer((req, res) => {
		if (req.url === "/bundle.js") { res.writeHead(200, {"Content-Type": "text/javascript"}); res.end(bundle); }
		else if (req.url === "/slow.png") {
			slowRequests++;
			res.writeHead(200, {"Content-Type": "image/png", "Content-Length": largeImage.length, "Cache-Control": "no-store"});
			res.write(largeImage.subarray(0, 256 * 1024)); pending.add(res); res.on("close", () => pending.delete(res));
		} else if (req.url === "/release-slow") {
			for (const response of pending) response.end(largeImage.subarray(256 * 1024));
			pending.clear(); res.end("released");
		} else if (req.url?.endsWith(".png")) { res.writeHead(200, {"Content-Type": "image/png", "Cache-Control": "no-store"}); res.end(smallImage); }
		else { res.writeHead(200, {"Content-Type": "text/html"}); res.end('<!doctype html><html><head></head><body><div id="app" style="width:720px"></div><script src="/bundle.js"></script></body></html>'); }
	});
	await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
	let context, profile;
	t.after(async () => {
		await context?.close();
		for (const response of pending) response.destroy();
		server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
		if (profile) await rm(profile, {recursive: true, force: true});
	});
	profile = await mkdtemp(path.join(tmpdir(), "openbear-markdown-test-"));
	context = await chromium.launchPersistentContext(profile, {executablePath, headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"], viewport: {width: 1000, height: 800}});
	const page = context.pages()[0];
	const pageErrors = []; page.on("pageerror", error => pageErrors.push(error.message));
	await page.goto(`http://127.0.0.1:${server.address().port}/`, {waitUntil: "domcontentloaded"});
	await page.waitForFunction(() => window.mdTest);

	await t.test("CJK strong emphasis works in both live and final output without altering code or escapes", async () => {
		const result = await page.evaluate(() => {
			const source = "**Vue 本来就有这项能力，但我们当前用了 v-html，绕开了 Vue 对里面子节点的比较。**Vue 看到的是“一整个 HTML 字符串变了”，于是直接设置 innerHTML。";
			return [false, true].map(live => {
				const render = text => window.mdTest.renderMarkdown(text, {live});
				return {original: render(source), quotes: render("前文**“中文”**后文"), inline: render("`**中文。**`后文"), fence: render("```md\n**中文。**后文\n```"), escaped: render("\\*\\*中文。\\*\\*后文"), incomplete: render("**中文。"), english: render("**English.**Next"), nested: render("**中文 *斜体*。**后文")};
			});
		});
		for (const row of result) {
			assert.match(row.original, /<strong>Vue 本来就有这项能力[^<]+比较。<\/strong>Vue/);
			assert.match(row.quotes, /<strong>“中文”<\/strong>/);
			assert.match(row.inline, /<code>\*\*中文。\*\*<\/code>/);
			assert.doesNotMatch(row.fence + row.escaped + row.incomplete + row.english, /<strong>/);
			assert.match(row.nested, /<strong>中文 <em>斜体<\/em>。<\/strong>/);
		}
	});

	await t.test("123 → 1234 updates the existing text node and leaves adjacent images untouched", async () => {
		const result = await page.evaluate(() => {
			const root = document.createElement("div"); document.body.append(root);
			window.mdTest.patchMarkdownDom(root, '<p>123</p><p><img src="/example.png" alt="test"></p>');
			const p = root.firstChild, text = p.firstChild, image = root.querySelector("img");
			window.mdTest.patchMarkdownDom(root, '<p>1234</p><p><img src="/example.png" alt="test"></p>');
			const result = {p: p === root.firstChild, text: text === p.firstChild, image: image === root.querySelector("img"), value: text.data}; root.remove(); return result;
		});
		assert.deepEqual(result, {p: true, text: true, image: true, value: "1234"});
	});

	await t.test("an unfinished large image retains its node and height throughout 20 streamed appends and finalization", async () => {
		await page.evaluate(async () => {
			const host = document.getElementById("app");
			const stats = window.imageStats = {insertions: 0, removals: 0, srcWrites: 0, loads: 0, heights: [], followingTops: []};
			host.addEventListener("load", event => { if (event.target.tagName === "IMG") stats.loads++; }, true);
			window.imageObserver = new MutationObserver(records => {
				for (const record of records) {
					if (record.type === "attributes" && record.target.tagName === "IMG") stats.srcWrites++;
					for (const [field, key] of [["addedNodes", "insertions"], ["removedNodes", "removals"]]) for (const node of record[field]) if (node.nodeType === 1) stats[key] += (node.matches("img") ? 1 : 0) + node.querySelectorAll("img").length;
				}
			}).observe(host, {childList: true, subtree: true, attributes: true, attributeFilter: ["src", "srcset"]});
			await window.mdTest.mount("![slow](/slow.png)\n\n后续", {live: true});
		});
		await page.waitForFunction(() => { const img = document.querySelector("#app img"); return img?.naturalHeight === 1200 && !img.complete; });
		const result = await page.evaluate(async () => {
			const img = document.querySelector("#app img"), stats = window.imageStats;
			let sampling = true;
			const sample = () => { if (!sampling) return; stats.heights.push(img.getBoundingClientRect().height); stats.followingTops.push(img.parentElement.nextElementSibling.getBoundingClientRect().top); requestAnimationFrame(sample); };
			sample();
			let text = "![slow](/slow.png)\n\n后续";
			for (let i = 0; i < 20; i++) { text += "文字"; await window.mdTest.update(text); await new Promise(resolve => setTimeout(resolve, 80)); }
			await window.mdTest.update(text, false);
			await new Promise(resolve => requestAnimationFrame(resolve));
			const stillLoading = !img.complete;
			await fetch("/release-slow"); await img.decode();
			await new Promise(resolve => requestAnimationFrame(resolve));
			sampling = false;
			return {...stats, stillLoading, sameImage: img === document.querySelector("#app img"), finalText: document.querySelector("#app .bear-md").textContent, caretCount: document.querySelectorAll("#app .md-live-caret").length};
		});
		assert.equal(result.stillLoading, true); assert.equal(result.sameImage, true);
		assert.equal(result.insertions, 1); assert.equal(result.removals, 0); assert.equal(result.srcWrites, 0); assert.equal(result.loads, 1);
		assert.ok(result.heights.length > 10); assert.ok(Math.min(...result.heights) > 0);
		assert.equal(new Set(result.heights).size, 1); assert.equal(new Set(result.followingTops).size, 1);
		assert.equal(result.caretCount, 0); assert.ok(result.finalText.includes("文字".repeat(20))); assert.equal(slowRequests, 1);
		t.diagnostic(`20 appends: image insertions=${result.insertions}, removals=${result.removals}, load=${result.loads}; ${result.heights.length} frames at stable ${result.heights[0]}px image height`);
	});

	await t.test("image preview still opens and closes after streaming", async () => {
		await page.locator("#app img").click();
		await page.waitForSelector("[data-test-viewer]");
		assert.match(await page.locator("[data-test-viewer]").getAttribute("data-test-viewer"), /\/slow\.png/);
		await page.locator("[data-test-viewer]").click();
		assert.equal(await page.locator("[data-test-viewer]").count(), 0);
	});

	await t.test("duplicate URLs stay distinct, wrapping reuses the image, and real removals/src changes are applied", async () => {
		const result = await page.evaluate(() => {
			const root = document.createElement("div"); document.body.append(root);
			const patch = text => window.mdTest.patchMarkdownDom(root, window.mdTest.renderMarkdown(text));
			patch("![one](/same.png)\n\n![two](/same.png)");
			const originals = [...root.querySelectorAll("img")];
			patch("![new](/new.png)\n\n[![one](/same.png)](https://example.test)\n\n![two](/same.png)\n\n尾部");
			const images = [...root.querySelectorAll("img")];
			const wrapped = images[1] === originals[0] && images[1].parentElement.tagName === "A";
			const distinct = images[1] !== images[2] && images[2] === originals[1];
			patch("![changed](/changed.png)");
			const removed = originals.every(image => !image.isConnected), changed = root.querySelector("img").getAttribute("src");
			patch(""); const empty = !root.hasChildNodes(); root.remove(); return {wrapped, distinct, removed, changed, empty};
		});
		assert.deepEqual(result, {wrapped: true, distinct: true, removed: true, changed: "/changed.png", empty: true});
	});

	await t.test("tables, code/highlighting, math SVG/MathML and link interactions survive tail changes", async () => {
		const result = await page.evaluate(async () => {
			const source = "| 表头 |\n| --- |\n| " + "long".repeat(300) + " |\n\n```js\nconst value = '" + "x".repeat(900) + "';\n```\n\n$\\sqrt{x} + \\frac{1}{2}$\n\n" + "[测试链接](https://example.test/)";
			await window.mdTest.mount(source);
			const root = document.querySelector("#app .bear-md"), table = root.querySelector(".md-table-scroll"), code = root.querySelector("pre"), math = root.querySelector(".katex"), link = root.querySelector("a");
			table.scrollLeft = 80; code.scrollLeft = 60;
			await window.mdTest.update(source + "\n\n**中文。**后文");
			let linkBubbled = false;
			root.addEventListener("click", event => { event.preventDefault(); linkBubbled = event.target === link; }, {once: true});
			link.click();
			let copyBubbled = false;
			root.parentElement.addEventListener("click", event => { copyBubbled = Boolean(event.target.closest(".md-code-copy")); }, {once: true});
			root.querySelector(".md-code-copy").click();
			return {same: table === root.querySelector(".md-table-scroll") && code === root.querySelector("pre") && math === root.querySelector(".katex") && link === root.querySelector("a"), tableScroll: table.scrollLeft, codeScroll: code.scrollLeft, highlighted: Boolean(code.querySelector(".hljs-keyword")), mathNamespace: root.querySelector("math")?.namespaceURI, svgNamespace: math.querySelector("svg")?.namespaceURI, linkBubbled, linkTarget: link.target, linkRel: link.rel, copyBubbled, html: root.innerHTML, expected: window.mdTest.renderMarkdown(source + "\n\n**中文。**后文")};
		});
		assert.equal(result.same, true); assert.equal(result.tableScroll, 80); assert.equal(result.codeScroll, 60); assert.equal(result.highlighted, true);
		assert.equal(result.mathNamespace, "http://www.w3.org/1998/Math/MathML"); assert.equal(result.svgNamespace, "http://www.w3.org/2000/svg");
		assert.equal(result.linkBubbled, true); assert.equal(result.linkTarget, "_blank"); assert.equal(result.linkRel, "noopener noreferrer"); assert.equal(result.copyBubbled, true);
		// Browser serialization normalizes empty attributes/self-closing tags; compare parsed trees instead.
		assert.equal(await page.evaluate(({html, expected}) => { const a = document.createElement("template"), b = document.createElement("template"); a.innerHTML = html; b.innerHTML = expected; return a.content.isEqualNode(b.content); }, result), true);
	});

	await t.test("streaming syntax can change structure and final highlighting without freezing stale markup", async () => {
		const result = await page.evaluate(async () => {
			await window.mdTest.mount("![pic](/syntax.png)\n\n**中文。", {live: true});
			const image = document.querySelector("#app img");
			await window.mdTest.update("![pic](/syntax.png)\n\n**中文。**后文\n\n```js\nconst n = 1;\n```", false);
			const root = document.querySelector("#app .bear-md");
			return {sameImage: image === root.querySelector("img"), strong: root.querySelector("strong")?.textContent, highlighted: Boolean(root.querySelector(".hljs-keyword")), caret: Boolean(root.querySelector(".md-live-caret")), unsafe: window.mdTest.renderMarkdown('<script>alert(1)</script>\n\n![x](javascript:alert(1))')};
		});
		assert.equal(result.sameImage, true); assert.equal(result.strong, "中文。"); assert.equal(result.highlighted, true); assert.equal(result.caret, false);
		assert.doesNotMatch(result.unsafe, /<script>|<img/);
	});
	await t.test("same-turn separators are 40% opaque in both themes without dimming message rules or shifting layout", async () => {
		const filename = path.join(webRoot, 'src/views/consoleView/TurnList.vue');
		const {descriptor} = parse(await readFile(filename, 'utf8'), {filename});
		const id = 'data-v-turn-list-test';
		const css = descriptor.styles.map(style => compileStyle({source: style.content, filename, id, scoped: style.scoped}).code).join('\n');
		const styleTag = await page.addStyleTag({content: css});
		try {
			for (const dark of [false, true]) {
				const result = await page.evaluate(async ({dark, id}) => {
					document.documentElement.classList.toggle('dark', dark);
					const app = document.getElementById('app'); app.className = 'assistant-card'; app.setAttribute(id, '');
					await window.mdTest.mount('----');
					const hr = app.querySelector('hr');
					const layout = () => { const c = getComputedStyle(hr), r = hr.getBoundingClientRect(); return {x:r.x, y:r.y, width:r.width, height:r.height, marginTop:c.marginTop, marginBottom:c.marginBottom, border:c.borderTop}; };
					hr.style.opacity = '1'; const before = layout(); hr.style.removeProperty('opacity');
					const after = layout(), opacity = getComputedStyle(hr).opacity;
					await window.mdTest.mount('第一段\n\n----\n\n第二段');
					const messageRule = getComputedStyle(app.querySelector('hr')).opacity;
					await window.mdTest.mount('----');
					const nested = document.createElement('div'); nested.className = 'timed-row'; app.append(nested); nested.append(app.querySelector('.bear-md'));
					const standaloneMessageRule = getComputedStyle(nested.querySelector('hr')).opacity;
					await window.mdTest.mount(''); nested.remove();
					return {before, after, opacity, messageRule, standaloneMessageRule};
				}, {dark, id});
				assert.equal(result.opacity, '0.4');
				assert.deepEqual(result.after, result.before);
				assert.equal(result.messageRule, '1');
				assert.equal(result.standaloneMessageRule, '1');
			}
		} finally {
			await styleTag.evaluate(el => el.remove());
			await page.evaluate(id => { document.documentElement.classList.remove('dark'); const app = document.getElementById('app'); app.className = ''; app.removeAttribute(id); }, id);
		}
	});
	assert.deepEqual(pageErrors, []);
});
