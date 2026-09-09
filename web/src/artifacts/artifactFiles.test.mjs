import assert from "node:assert/strict";
import test, {afterEach} from "node:test";
import {artifactFromUrl, artifactFormat, formatFileSize, extractArtifactSummary, decodeArtifactText, artifactRecord, loadArtifactMetadata, loadArtifactText, loadArtifactCard, clearArtifactCache, readingState, SUMMARY_LIMIT, TEXT_PREVIEW_LIMIT} from "./artifactFiles.js";

const origin = "https://openbear.test";
const conversationUuid = "39a541d4-4d9c-4a58-87d6-1b276779954a", artifactUuid = "adfead18-e6d1-40df-8475-391e1245aa0d";
const href = `/api/conversations/${conversationUuid}/artifacts/${artifactUuid}/content`;
const identity = artifactFromUrl(href, origin);
const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; clearArtifactCache(); });
const metadata = (extra = {}) => ({conversationUuid, artifactUuid, fileName: "方案.md", mimeType: "text/markdown", sizeBytes: 200, sha256: "test-digest", ...extra});
function fakeFetch(meta, text = "# 实际标题\n\n这里是实际正文。") {
	const calls = [];
	globalThis.fetch = async (url, options) => { calls.push({url, options}); return url.endsWith("/content") ? new Response(text) : Response.json({ok: true, artifact: meta}); };
	return calls;
}

test("only same-origin canonical artifact paths become preview identities, using the linked conversation", () => {
	assert.equal(identity.conversationUuid, conversationUuid);
	assert.equal(identity.metadataUrl, href.slice(0, -8));
	assert.equal(identity.downloadUrl, href + "?download=1");
	assert.equal(artifactFromUrl(origin + href + "?preview=1", origin).key, identity.key);
	assert.equal(artifactFromUrl(href + "?download=true", origin).download, true);
	for (const value of ["https://other.test" + href, "//other.test" + href, "https://user:pass@openbear.test" + href, "javascript:alert(1)", "file:///tmp/file.md", "/api/conversations/x/artifacts/y/content", href + "/other", "[file](" + href + ")"]) assert.equal(artifactFromUrl(value, origin), null, value);
});

for (const [fileName, mimeType, kind, language] of [
	["方案.MD", "text/plain", "markdown", "markdown"], ["log.txt", "text/plain", "text", ""], ["data.json", "application/json", "code", "json"], ["a.yaml", "application/octet-stream", "code", "yaml"], ["script.py", "text/x-python", "code", "python"],
	["picture.png", "image/png", "image", ""], ["page.html", "text/html", "code", "xml"], ["picture.svg", "image/png", "code", "xml"], ["innocent.png", "image/svg+xml", "code", "xml"], ["not-a-picture.md", "image/png", "unsupported", ""], ["archive.zip", "application/zip", "unsupported", ""], ["doc.pdf", "application/pdf", "unsupported", ""],
]) test(`file classification: ${fileName} / ${mimeType}`, () => assert.deepEqual([artifactFormat({fileName, mimeType}).kind, artifactFormat({fileName, mimeType}).language], [kind, language]));

test("missing metadata is a valid loading/error state", () => {
	assert.equal(artifactFormat(null).kind, "unsupported"); assert.equal(artifactFormat().kind, "unsupported");
});

test("clearing the viewer cache invalidates old in-flight cards before they fetch document contents", async () => {
	let release;
	const calls = [];
	globalThis.fetch = async url => { calls.push(url); return new Promise(resolve => { release = resolve; }); };
	const pending = loadArtifactCard(artifactRecord(identity));
	clearArtifactCache(); release(Response.json({ok: true, artifact: metadata()}));
	await assert.rejects(pending, /读取已取消/); assert.equal(calls.length, 1);
	fakeFetch(metadata()); await loadArtifactCard(artifactRecord(identity));
});

test("summary comes from real Markdown text without markup, code samples or invented facts", () => {
	assert.deepEqual(extractArtifactSummary("# **真实标题。**后文\n\n正文包含 **加粗** 与 `代码`。\n\n下一段", "markdown"), {title: "真实标题。后文", excerpt: "正文包含 加粗 与 代码。"});
	assert.deepEqual(extractArtifactSummary("```md\n# 不是标题\n```\n\n# 标题\n\n内容", "markdown"), {title: "标题", excerpt: "内容"});
	assert.equal(extractArtifactSummary("# 标题", "markdown").excerpt, "");
	assert.equal(extractArtifactSummary("a\nb\nc\nd", "code").excerpt, "a b c");
	assert.equal(formatFileSize(0), "0 B"); assert.equal(formatFileSize(1024), "1.0 KB");
});

test("text decoding preserves source including literal HTML and rejects binary/unknown encodings", () => {
	assert.equal(decodeArtifactText(new TextEncoder().encode("<script>x</script>\n**原文**")), "<script>x</script>\n**原文**");
	assert.equal(decodeArtifactText(Uint8Array.from([255, 254, 65, 0])), "A");
	assert.throws(() => decodeArtifactText(Uint8Array.from([0, 1, 2, 3])), /不像可读文本/);
	assert.throws(() => decodeArtifactText(Uint8Array.from([255, 255])), /文本编码/);
});

test("cards and viewer share metadata/text requests and use canonical authenticated URLs", async () => {
	const calls = fakeFetch(metadata({contentUrl: "https://evil.test/leak", downloadUrl: "https://evil.test/leak"}));
	const record = artifactRecord(identity);
	await Promise.all([loadArtifactCard(record), loadArtifactCard(record), loadArtifactText(record)]);
	assert.equal(calls.filter(call => call.url === identity.metadataUrl).length, 1);
	assert.equal(calls.filter(call => call.url === identity.contentUrl).length, 1);
	assert.ok(calls.every(call => call.options.credentials === "same-origin" && call.options.redirect === "error"));
	assert.equal(record.summary.title, "实际标题");
	await loadArtifactText(record); assert.equal(calls.length, 2);
});

test("large text, image and unsupported cards only fetch metadata; content loads on demand", async () => {
	for (const meta of [metadata({sizeBytes: SUMMARY_LIMIT + 1}), metadata({fileName: "a.png", mimeType: "image/png"}), metadata({fileName: "a.zip", mimeType: "application/zip"})]) {
		clearArtifactCache(); const calls = fakeFetch(meta); await loadArtifactCard(artifactRecord(identity)); assert.equal(calls.length, 1);
	}
	clearArtifactCache(); const calls = fakeFetch(metadata({sizeBytes: TEXT_PREVIEW_LIMIT + 1}));
	await assert.rejects(loadArtifactText(artifactRecord(identity)), /预览上限/); assert.equal(calls.length, 1);
});

test("actual response limits are enforced even if metadata or Content-Length is missing/wrong", async () => {
	fakeFetch(metadata(), "x".repeat(TEXT_PREVIEW_LIMIT + 1));
	await assert.rejects(loadArtifactText(artifactRecord(identity)), /预览上限/);
});

test("a forced 404 invalidates cached metadata and retry can recover without showing stale content", async () => {
	fakeFetch(metadata()); const record = artifactRecord(identity); await loadArtifactCard(record);
	globalThis.fetch = async () => new Response("missing", {status: 404});
	await assert.rejects(loadArtifactMetadata(record, {force: true}), /不存在/);
	assert.equal(record.metadata, null); assert.equal(record.summary, null);
	await assert.rejects(loadArtifactMetadata(record), /不存在/);
	fakeFetch(metadata({sha256: "new"}), "# 新内容"); await loadArtifactCard(record); assert.equal(record.summary.title, "新内容");
});

test("mismatched identity metadata cannot substitute another document", async () => {
	fakeFetch(metadata({artifactUuid: "wrong"}));
	await assert.rejects(loadArtifactMetadata(artifactRecord(identity)), /信息不完整/);
});

test("text cache is bounded and reading states are separate per file/version", async () => {
	const a = readingState("a:v1"); a.previewTop = 345; a.mode = "source";
	assert.equal(readingState("a:v1"), a); assert.equal(readingState("a:v2").previewTop, 0);
	const calls = fakeFetch(metadata({sizeBytes: 1024 * 1024}), "x".repeat(1024 * 1024));
	const record = artifactRecord(identity);
	for (let i = 0; i < 10; i++) { record.metadata = metadata({sizeBytes: 1024 * 1024, sha256: `version-${i}`}); record.checkedAt = Date.now(); await loadArtifactText(record); }
	assert.equal(calls.length, 10);
	record.metadata = {...record.metadata, sha256: "version-0"}; await loadArtifactText(record); assert.equal(calls.length, 11);
});
