import {shallowReactive} from "vue";
import MarkdownIt from "markdown-it";
import cjkFriendly from "markdown-it-cjk-friendly";

export const SUMMARY_LIMIT = 96 * 1024;
export const TEXT_PREVIEW_LIMIT = 2 * 1024 * 1024;
const TEXT_CACHE_LIMIT = 8 * 1024 * 1024;
const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
const ARTIFACT_PATH = new RegExp(`^/api/conversations/(${UUID})/artifacts/(${UUID})/content$`, "i");
const records = new Map(), texts = new Map(), readingStates = new Map();
let cachedBytes = 0, cacheGeneration = 0;
const summaryMarkdown = new MarkdownIt({html: false}).use(cjkFriendly);
const CODE_EXTENSIONS = {
	js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript", ts: "typescript", tsx: "typescript",
	json: "json", jsonl: "json", ndjson: "json", yaml: "yaml", yml: "yaml", toml: "ini", ini: "ini", conf: "ini",
	py: "python", sh: "bash", bash: "bash", zsh: "bash", ps1: "powershell", sql: "sql", css: "css", scss: "scss",
	html: "xml", htm: "xml", svg: "xml", xml: "xml", vue: "xml", svelte: "xml", go: "go", rs: "rust", java: "java",
	c: "c", h: "c", cpp: "cpp", hpp: "cpp", cs: "csharp", rb: "ruby", php: "php", swift: "swift", kt: "kotlin",
	diff: "diff", patch: "diff", dockerfile: "dockerfile", makefile: "makefile",
};
const CODE_MIMES = {"application/json": "json", "application/x-jsonlines": "json", "application/yaml": "yaml", "application/x-yaml": "yaml", "application/xml": "xml", "application/javascript": "javascript", "application/typescript": "typescript", "application/sql": "sql"};
const SAFE_IMAGES = new Set(["png", "jpg", "jpeg", "gif", "webp", "avif", "bmp", "ico"]);
const SAFE_IMAGE_MIMES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp", "image/x-icon", "image/vnd.microsoft.icon"]);

export function artifactFromUrl(value, origin = globalThis.location?.origin) {
	if (!origin || typeof value !== "string") return null;
	try {
		const url = new URL(value, origin);
		if (url.origin !== origin || url.username || url.password || !["http:", "https:"].includes(url.protocol)) return null;
		const match = url.pathname.match(ARTIFACT_PATH);
		if (!match) return null;
		const [, conversationUuid, artifactUuid] = match;
		const contentUrl = `/api/conversations/${conversationUuid}/artifacts/${artifactUuid}/content`;
		return {conversationUuid, artifactUuid, key: `${conversationUuid}:${artifactUuid}`, contentUrl, metadataUrl: contentUrl.slice(0, -8), downloadUrl: `${contentUrl}?download=1`, download: ["1", "true", "yes", "on"].includes((url.searchParams.get("download") || "").toLowerCase())};
	} catch { return null; }
}

// Only the server-validated workspace path is shareable with another conversation.
// In particular, never copy a content URL or a blob storage path as a substitute.
export function artifactSharedPath(metadata) {
	const path = metadata?.workspacePath;
	if (typeof path !== "string" || !path.startsWith("workspace/artifacts/")) return "";
	const parts = path.slice("workspace/artifacts/".length).split("/");
	return parts.every(part => part && part !== "." && part !== ".." && !/[\\\x00-\x1f\x7f]/.test(part)) ? path : "";
}

export function artifactFormat(metadata = {}) {
	metadata = metadata || {};
	const name = String(metadata.fileName || "").toLowerCase();
	const ext = name.includes(".") ? name.split(".").pop() : name;
	const mime = String(metadata.mimeType || "").split(";")[0].trim().toLowerCase();
	// Never send active formats to the ordinary image renderer, even if mislabeled.
	if (ext === "svg" || mime === "image/svg+xml") return {kind: "code", label: "SVG 源码", language: "xml", sourceOnly: true};
	if (["html", "htm"].includes(ext) || ["text/html", "application/xhtml+xml"].includes(mime)) return {kind: "html", label: "HTML", language: "xml"};
	const generic = !mime || mime === "application/octet-stream";
	const textMime = generic || mime.startsWith("text/") || Boolean(CODE_MIMES[mime]) || mime === "application/toml";
	if ((SAFE_IMAGES.has(ext) && (generic || SAFE_IMAGE_MIMES.has(mime))) || SAFE_IMAGE_MIMES.has(mime) && !CODE_EXTENSIONS[ext] && !["md", "markdown", "txt"].includes(ext)) return {kind: "image", label: "图片", language: ""};
	if ((["md", "markdown", "mdown"].includes(ext) && textMime) || ["text/markdown", "text/x-markdown"].includes(mime)) return {kind: "markdown", label: "Markdown", language: "markdown"};
	if (CODE_EXTENSIONS[ext] && textMime) return {kind: "code", label: ext.toUpperCase(), language: CODE_EXTENSIONS[ext]};
	if (CODE_MIMES[mime]) return {kind: "code", label: CODE_MIMES[mime].toUpperCase(), language: CODE_MIMES[mime]};
	if (mime.startsWith("text/") || generic && ["txt", "log", "csv", "tsv", "rst"].includes(ext)) return {kind: "text", label: ext ? ext.toUpperCase() : "文本", language: ""};
	return {kind: "unsupported", label: ext && ext.length < 12 ? ext.toUpperCase() : "文件", language: ""};
}

export function formatFileSize(bytes) {
	const value = Number(bytes);
	if (!Number.isFinite(value) || value < 0) return "大小未知";
	if (value < 1024) return `${value} B`;
	if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
	return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

export function extractArtifactSummary(text, kind) {
	if (kind !== "markdown") return {title: "", excerpt: String(text).split(/\r?\n/).filter(line => line.trim()).slice(0, 3).join(" ").slice(0, 180)};
	const tokens = summaryMarkdown.parse(String(text), {});
	const plain = token => (token?.children || []).map(child => ["text", "code_inline", "image"].includes(child.type) ? child.content : ["softbreak", "hardbreak"].includes(child.type) ? " " : "").join("").trim();
	let title = "", excerpt = "";
	for (let i = 0; i < tokens.length; i++) {
		if (!title && tokens[i].type === "heading_open" && tokens[i].tag === "h1") title = plain(tokens[i + 1]);
		if (!excerpt && tokens[i].type === "paragraph_open") excerpt = plain(tokens[i + 1]);
		if (title && excerpt) break;
	}
	return {title: title.slice(0, 160), excerpt: excerpt.slice(0, 180)};
}

function touch(map, key, value, limit = 128) {
	map.delete(key); map.set(key, value);
	while (map.size > limit) map.delete(map.keys().next().value);
	return value;
}
export function artifactRecord(identity) {
	if (records.has(identity.key)) return touch(records, identity.key, records.get(identity.key));
	return touch(records, identity.key, shallowReactive({identity, cacheGeneration, metadata: null, summary: null, metadataError: "", busy: false, checkedAt: 0, metadataRequest: null, textRequest: null}));
}
export function readingState(key, initialMode = "preview") {
	return touch(readingStates, key, readingStates.get(key) || {mode: initialMode, previewTop: 0, sourceTop: 0, wrap: true});
}
export function clearArtifactCache() { cacheGeneration++; records.clear(); texts.clear(); readingStates.clear(); cachedBytes = 0; }

export function artifactErrorMessage(error) {
	if (error?.name === "AbortError") return "读取超时，请重试或下载原文件。";
	return error?.message || "附件暂时无法读取，请重试。";
}
function responseError(status) {
	if (status === 401) return new Error("登录已失效，请刷新页面后重试。");
	if (status === 403 || status === 404) return new Error("附件不存在、已删除，或当前无权访问。");
	return new Error(`读取附件失败（HTTP ${status}），请重试。`);
}
async function request(url, consume) {
	const controller = new AbortController();
	const timer = setTimeout(() => controller.abort(), 30000);
	try {
		const response = await fetch(url, {credentials: "same-origin", signal: controller.signal, redirect: "error"});
		if (!response.ok) throw responseError(response.status);
		return await consume(response);
	} finally { clearTimeout(timer); }
}

function assertCurrentRecord(record) {
	if (record.cacheGeneration !== cacheGeneration) throw new Error("附件读取已取消。");
}
export async function loadArtifactMetadata(record, {force = false} = {}) {
	assertCurrentRecord(record);
	if (record.metadataRequest) return record.metadataRequest;
	if (!force && record.metadata && Date.now() - record.checkedAt < 60000) return record.metadata;
	record.busy = true; record.metadataError = "";
	const promise = request(record.identity.metadataUrl, response => response.json()).then(result => {
		assertCurrentRecord(record);
		const meta = result?.artifact;
		if (!meta || meta.artifactUuid !== record.identity.artifactUuid || meta.conversationUuid !== record.identity.conversationUuid || !Number.isSafeInteger(meta.sizeBytes) || meta.sizeBytes < 0) throw new Error("附件信息不完整，暂时无法预览。");
		if (record.metadata?.sha256 !== meta.sha256) record.summary = null;
		record.metadata = meta; record.checkedAt = Date.now(); return meta;
	}).catch(error => { record.checkedAt = 0; record.metadata = null; record.summary = null; record.metadataError = artifactErrorMessage(error); throw error; }).finally(() => { record.busy = false; record.metadataRequest = null; });
	record.metadataRequest = promise;
	return promise;
}

export function decodeArtifactText(bytes) {
	let encoding = "utf-8";
	if (bytes[0] === 0xff && bytes[1] === 0xfe) encoding = "utf-16le";
	if (bytes[0] === 0xfe && bytes[1] === 0xff) encoding = "utf-16be";
	let text;
	try { text = new TextDecoder(encoding, {fatal: true}).decode(bytes); }
	catch { throw new Error("无法识别文件的文本编码，请下载原文件查看。"); }
	if (/\x00/.test(text) || (text.match(/[\x01-\x08\x0b\x0e-\x1f]/g)?.length || 0) > Math.max(2, text.length / 100)) throw new Error("文件内容不像可读文本，请下载原文件查看。");
	return text;
}
async function readTextResponse(response) {
	if (Number(response.headers.get("content-length")) > TEXT_PREVIEW_LIMIT) { await response.body?.cancel(); throw new Error("文件超过 2 MB 的文本预览上限，请下载查看。"); }
	const reader = response.body.getReader();
	const chunks = []; let total = 0;
	try {
		while (true) {
			const {done, value} = await reader.read(); if (done) break;
			total += value.byteLength;
			if (total > TEXT_PREVIEW_LIMIT) { await reader.cancel(); throw new Error("文件超过 2 MB 的文本预览上限，请下载查看。"); }
			chunks.push(value);
		}
	} finally { reader.releaseLock(); }
	const bytes = new Uint8Array(total); let offset = 0;
	for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
	return {text: decodeArtifactText(bytes), bytes: total};
}
export async function loadArtifactText(record) {
	const meta = await loadArtifactMetadata(record);
	assertCurrentRecord(record);
	const format = artifactFormat(meta);
	if (!["markdown", "html", "text", "code"].includes(format.kind)) throw new Error("此格式暂不支持文本预览，请下载原文件。");
	if (meta.sizeBytes > TEXT_PREVIEW_LIMIT) throw new Error("文件超过 2 MB 的文本预览上限，请下载查看。");
	const key = `${record.identity.key}:${meta.sha256 || ""}`;
	if (texts.has(key)) { const cached = texts.get(key); texts.delete(key); texts.set(key, cached); return cached.text; }
	if (record.textRequest) return record.textRequest;
	const currentGeneration = cacheGeneration;
	const promise = request(record.identity.contentUrl, readTextResponse).then(value => {
		if (currentGeneration !== cacheGeneration) return value.text;
		texts.set(key, value); cachedBytes += value.bytes;
		while (cachedBytes > TEXT_CACHE_LIMIT || texts.size > 64) { const oldest = texts.keys().next().value; cachedBytes -= texts.get(oldest).bytes; texts.delete(oldest); }
		if (format.kind !== "html") record.summary = extractArtifactSummary(value.text, format.kind);
		return value.text;
	}).finally(() => { record.textRequest = null; });
	record.textRequest = promise;
	return promise;
}
export async function loadArtifactCard(record) {
	const meta = await loadArtifactMetadata(record);
	if (["markdown", "text", "code"].includes(artifactFormat(meta).kind) && meta.sizeBytes <= SUMMARY_LIMIT && !record.summary) {
		// A missing excerpt must never make an otherwise downloadable attachment unusable.
		await loadArtifactText(record).catch(() => {});
	}
	return meta;
}

export function openArtifactPreview(identity, label = "", opener = null) {
	window.dispatchEvent(new CustomEvent("openbear:preview-artifact", {detail: {href: identity.contentUrl, label, opener}}));
}
