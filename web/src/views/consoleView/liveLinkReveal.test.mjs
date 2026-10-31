import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";

// Execute the component's actual live-typing helpers, not a parallel copy.
const source = readFileSync(new URL("./ConsoleMarkdown.vue", import.meta.url), "utf8");
const constants = source.slice(source.indexOf("const SOFT_PUNCT"), source.indexOf("const componentTag"));
const helpers = source.slice(source.indexOf("function codeFenceOpenAt"), source.indexOf("function paintLiveText"));
const nextLiveSlice = new Function(`${constants}
let fenceCacheSource = "", fenceCacheIndex = 0, fenceCacheCount = 0;
${helpers}
return nextLiveSlice;`)();

function slices(target, from = "") {
	const seen = [];
	let current = from;
	for (let i = 0; i < 2000 && current !== target; i++) {
		const next = nextLiveSlice(current, target);
		if (next === current) break;
		seen.push(next);
		current = next;
	}
	return seen;
}

test("a complete link destination is revealed in one step, never typed out", () => {
	const url = "/api/conversations/c/artifacts/a/content?preview=1";
	const target = `看图：\n\n![预览](${url})\n\n后面还有一段文字，继续输出。`;
	const seen = slices(target);
	assert.equal(seen.at(-1), target);
	const open = target.indexOf("](") + 2;
	const close = target.indexOf(")", open) + 1;
	for (const slice of seen) assert.ok(slice.length <= open || slice.length >= close, `partial destination: ${slice.slice(open)}`);
});

test("an unfinished destination holds at its start until the stream supplies it", () => {
	const target = "看图：\n\n![预览](workspace/artifacts/x.pn";
	const seen = slices(target);
	assert.equal(seen.at(-1), "看图：\n\n![预览](");
	assert.equal(nextLiveSlice("看图：\n\n![预览](", target), "看图：\n\n![预览](");
});

test("bracket text that is not a link keeps typing normally", () => {
	const target = "数组写法 a](b c 之后的说明文字";
	assert.equal(slices(target).at(-1), target);
	const code = "```js\nconst x = y](z;\nfoo()\n```";
	assert.equal(slices(code).at(-1), code);
});
