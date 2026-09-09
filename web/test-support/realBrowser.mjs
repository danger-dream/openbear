import {accessSync, constants, readdirSync, statSync} from "node:fs";
import {readFile} from "node:fs/promises";
import {homedir} from "node:os";
import {createHash} from "node:crypto";
import {crc32, deflateSync} from "node:zlib";
import path from "node:path";
import {fileURLToPath} from "node:url";
import {build} from "esbuild";
import {parse, compileScript, compileStyle, compileTemplate} from "@vue/compiler-sfc";
import {chromium} from "playwright-core";

export const webRoot = fileURLToPath(new URL("../", import.meta.url));
const SYSTEM_BROWSERS = ['/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/opt/google/chrome/chrome'];
function isExecutable(file) {
	try { accessSync(file, constants.X_OK); return statSync(file).isFile(); } catch { return false; }
}
export function browserExecutable({env = process.env, home = homedir(), systemPaths = SYSTEM_BROWSERS, playwrightPath = chromium.executablePath()} = {}) {
	// CI provisions Chrome before npm test and supplies its exact path. Never
	// hide a broken explicit setup by quietly falling back to a developer cache.
	if (env.CHROME_BIN) {
		if (!isExecutable(env.CHROME_BIN)) throw new Error(`CHROME_BIN is not an executable browser: ${env.CHROME_BIN}`);
		return env.CHROME_BIN;
	}
	const cache = path.join(home, '.cache/ms-playwright');
	let names = [];
	try { names = readdirSync(cache); } catch { /* A fresh machine has no cache. */ }
	const cached = names.filter(name => /^chromium(?:_headless_shell)?-/.test(name)).sort((a,b) => b.localeCompare(a, undefined, {numeric:true})).flatMap(name => [
		path.join(cache, name, 'chrome-headless-shell-linux64/chrome-headless-shell'),
		path.join(cache, name, 'chrome-linux64/chrome'),
		path.join(cache, name, 'chrome-linux/chrome'),
	]);
	const found = [playwrightPath, ...systemPaths, ...cached].find(file => file && isExecutable(file));
	if (!found && /^(true|1)$/i.test(String(env.CI || ''))) {
		throw new Error('Browser tests require Chrome in CI. Install Chrome before npm test and set CHROME_BIN to its executable path.');
	}
	return found;
}
export function testPng(width, height) {
	const chunk = (type, bytes) => {
		const body = Buffer.concat([Buffer.from(type), bytes]), head = Buffer.alloc(4), tail = Buffer.alloc(4);
		head.writeUInt32BE(bytes.length); tail.writeUInt32BE(crc32(body));
		return Buffer.concat([head, body, tail]);
	};
	const header = Buffer.alloc(13); header.writeUInt32BE(width, 0); header.writeUInt32BE(height, 4); header[8] = 8; header[9] = 2;
	const pixels = Buffer.alloc((width * 3 + 1) * height, 140);
	for (let row = 0; row < height; row++) pixels[row * (width * 3 + 1)] = 0;
	return Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]), chunk("IHDR", header), chunk("IDAT", deflateSync(pixels, {level: 0})), chunk("IEND", Buffer.alloc(0))]);
}
export async function componentBundle(entry) {
	const result = await build({
		stdin: {contents: entry, resolveDir: webRoot}, bundle: true, write: false, format: "iife", platform: "browser",
		define: {"process.env.NODE_ENV": '"production"'}, loader: {".css": "empty"},
		plugins: [{name: "real-vue-sfc", setup(builder) {
			builder.onLoad({filter: /\.vue$/}, async args => {
				const {descriptor} = parse(await readFile(args.path, "utf8"), {filename: args.path});
				const id = "data-v-" + createHash("sha256").update(args.path).digest("hex").slice(0, 10);
				const compiled = descriptor.script || descriptor.scriptSetup
					? compileScript(descriptor, {id, genDefaultAs: "TestComponent", inlineTemplate: true})
					: {content: 'const TestComponent = {};', bindings: {}};
				let component = compiled.content;
				if (!descriptor.scriptSetup && descriptor.template) {
					const template = compileTemplate({source: descriptor.template.content, filename: args.path, id, compilerOptions: {bindingMetadata: compiled.bindings}});
					if (template.errors.length) throw template.errors[0];
					component += '\n' + template.code.replace('export function render', 'function render') + '\nTestComponent.render = render;';
				}
				const css = descriptor.styles.map(style => {
					const result = compileStyle({source: style.content, filename: args.path, id, scoped: style.scoped});
					if (result.errors.length) throw result.errors[0];
					return result.code;
				}).join("\n");
				return {contents: `${component}\nTestComponent.__scopeId = '${id}';\nconst style = document.createElement('style'); style.textContent = ${JSON.stringify(css)}; document.head.append(style);\nexport default TestComponent;`, loader: "js", resolveDir: path.dirname(args.path)};
			});
		}}],
	});
	return result.outputFiles[0].contents;
}
