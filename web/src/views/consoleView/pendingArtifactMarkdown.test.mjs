import test from "node:test";
import assert from "node:assert/strict";
import {renderMarkdown} from "../../references/rendererHarness.mjs";

const source = "预览：\n\n[preview.html](workspace/artifacts/nav/preview.html)\n\n![浅色 <280>](./workspace/artifacts/nav/light.png)\n\n[外链](https://example.com/a.png)";

test("streaming segment shows unresolved workspace artifacts as pending chips, not requests", () => {
	const html = renderMarkdown(source, {live: true});
	assert.doesNotMatch(html, /<img/);
	assert.doesNotMatch(html, /href="(?:\.\/)?workspace\/artifacts/);
	assert.match(html, /<span class="md-artifact-pending"[^>]*>preview\.html<\/span>/);
	assert.match(html, /<span class="md-artifact-pending is-image">图片准备中：浅色 &lt;280&gt;<\/span>/);
	assert.match(html, /<a href="https:\/\/example\.com\/a\.png"/);
});

test("completed and rewritten segments keep ordinary images and links", () => {
	const finished = renderMarkdown(source);
	assert.doesNotMatch(finished, /md-artifact-pending/);
	assert.match(finished, /<img src="\.\/workspace\/artifacts\/nav\/light\.png"/);
	const rewritten = renderMarkdown("![图](/api/conversations/c/artifacts/a/content?preview=1)", {live: true});
	assert.match(rewritten, /<img src="\/api\/conversations\/c\/artifacts\/a\/content\?preview=1"/);
	assert.doesNotMatch(renderMarkdown(source, {highlight: false}), /md-artifact-pending/);
});
