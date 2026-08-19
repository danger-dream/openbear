import MarkdownIt from "markdown-it";
import texmath from "markdown-it-texmath";
import katex from "katex";
import hljs from "highlight.js";
import "highlight.js/styles/github.css";
import "katex/dist/katex.min.css";
import "markdown-it-texmath/css/texmath.css";

const MAX_HIGHLIGHT_CHARS = 8000;
const MARKDOWN_CACHE_LIMIT = 220;
const MARKDOWN_CACHE_MAX_SOURCE_CHARS = 120000;
const PLAIN_TEXT_CACHE_LIMIT = 500;
const PLAIN_TEXT_CACHE_MAX_SOURCE_CHARS = 80000;
const markdownCache = new Map();
const plainTextCache = new Map();

export function escapeHtmlText(value) {
	return String(value ?? "")
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;")
		.replace(/"/g, "&quot;")
		.replace(/'/g, "&#039;");
}

export function highlightCodeHtml(code, lang = "") {
	const source = String(code ?? "");
	const language = String(lang || "").trim().split(/\s+/)[0];
	try {
		if (language && hljs.getLanguage(language) && source.length <= MAX_HIGHLIGHT_CHARS) {
			return hljs.highlight(source, {language}).value;
		}
	} catch {
		// Safe escaped text is the reliable fallback for unknown grammars.
	}
	return escapeHtmlText(source);
}

function createMarkdownRenderer({highlightCode = true} = {}) {
	let renderer;
	renderer = new MarkdownIt({
		html: false,
		linkify: true,
		breaks: true,
		typographer: true,
		highlight(code, lang) {
			try {
				const language = String(lang || "").trim().split(/\s+/)[0];
				if (highlightCode && language && hljs.getLanguage(language) && String(code || "").length <= MAX_HIGHLIGHT_CHARS) {
					return hljs.highlight(code, {language}).value;
				}
				return renderer.utils.escapeHtml(code);
			} catch {
				return renderer.utils.escapeHtml(code);
			}
		},
	});
	renderer.use(texmath, {
		engine: katex,
		delimiters: ["dollars", "brackets"],
		katexOptions: {throwOnError: false, strict: "ignore"},
	});

	function renderCodeBlock(code, lang = "") {
		const source = String(code || "");
		const language = String(lang || "").trim().split(/\s+/)[0];
		const canHighlight = Boolean(highlightCode && language && hljs.getLanguage(language) && source.length <= MAX_HIGHLIGHT_CHARS);
		const label = language || "代码块";
		const escapedLabel = renderer.utils.escapeHtml(label);
		const className = language ? ` language-${renderer.utils.escapeHtml(language)}` : "";
		let highlighted;
		try {
			highlighted = canHighlight ? hljs.highlight(source, {language}).value : renderer.utils.escapeHtml(source);
		} catch {
			highlighted = renderer.utils.escapeHtml(source);
		}
		const muted = highlightCode && source.length > MAX_HIGHLIGHT_CHARS ? `<em>已跳过高亮，避免大代码块卡顿</em>` : "";
		return `<div class="md-code-block"><div class="md-code-head"><span>${escapedLabel}</span>${muted}<button type="button" class="md-code-copy" title="复制代码">复制</button></div><pre><code class="hljs${className}">${highlighted}</code></pre></div>`;
	}

	renderer.renderer.rules.fence = (tokens, idx) => renderCodeBlock(tokens[idx].content || "", tokens[idx].info || "");
	renderer.renderer.rules.code_block = (tokens, idx) => renderCodeBlock(tokens[idx].content || "", "");
	renderer.renderer.rules.link_open = (tokens, idx, options, _env, self) => {
		const token = tokens[idx];
		token.attrSet("target", "_blank");
		token.attrSet("rel", "noopener noreferrer");
		return self.renderToken(tokens, idx, options);
	};
	renderer.renderer.rules.table_open = () => `<div class="md-table-scroll"><table>`;
	renderer.renderer.rules.table_close = () => `</table></div>`;
	return renderer;
}

const md = createMarkdownRenderer({highlightCode: true});
const liveMd = createMarkdownRenderer({highlightCode: false});

function touchCache(cache, key, value, limit) {
	cache.delete(key);
	cache.set(key, value);
	while (cache.size > limit) cache.delete(cache.keys().next().value);
	return value;
}

export function decodeHtml(text) {
	if (!text || !/[&<]/.test(text)) return String(text || "");
	const el = document.createElement("textarea");
	el.innerHTML = String(text || "");
	return el.value;
}

function inlineCode(text) {
	const raw = decodeHtml(text).replace(/`/g, "ˋ");
	return `\`${raw}\``;
}

let markdownCodePlaceholderSeq = 0;
const FENCED_MARKDOWN_CODE_RE = /(^|\n)([ \t]*)(`{3,}|~{3,})[^\n]*(?:\n[\s\S]*?)(?:\n[ \t]*\3[ \t]*(?=\n|$)|$)/g;
const INLINE_MARKDOWN_CODE_RE = /(`+)[^\n]*?\1/g;

function escapeRegExp(value) {
	return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function protectMarkdownCode(text) {
	const slots = [];
	const prefix = `\uE000OPENBEAR_MD_CODE_${markdownCodePlaceholderSeq++}_`;
	const suffix = "_\uE000";
	const stash = (raw) => {
		const key = `${prefix}${slots.length}${suffix}`;
		slots.push(raw);
		return key;
	};
	let s = String(text || "");
	s = s.replace(FENCED_MARKDOWN_CODE_RE, stash);
	s = s.replace(INLINE_MARKDOWN_CODE_RE, stash);
	const slotRe = new RegExp(`${escapeRegExp(prefix)}(\\d+)${escapeRegExp(suffix)}`, "g");
	return {
		text: s,
		restore(value) {
			return String(value || "").replace(slotRe, (_, index) => slots[Number(index)] ?? "");
		},
	};
}

export function richHtmlToMarkdown(text) {
	const protectedMarkdown = protectMarkdownCode(String(text || ""));
	let s = protectedMarkdown.text;
	s = s.replace(/<br\s*\/?\s*>/gi, "\n");
	s = s.replace(/<pre><code[^>]*>([\s\S]*?)<\/code><\/pre>/gi, (_, code) => `\n\n\`\`\`\n${decodeHtml(code)}\n\`\`\`\n\n`);
	s = s.replace(/<pre[^>]*>([\s\S]*?)<\/pre>/gi, (_, code) => `\n\n\`\`\`\n${decodeHtml(code)}\n\`\`\`\n\n`);
	s = s.replace(/<code[^>]*>([\s\S]*?)<\/code>/gi, (_, code) => inlineCode(code));
	const protectedGeneratedCode = protectMarkdownCode(s);
	s = protectedGeneratedCode.text;
	s = s.replace(/<(b|strong)>([\s\S]*?)<\/\1>/gi, "**$2**");
	s = s.replace(/<(i|em)>([\s\S]*?)<\/\1>/gi, "*$2*");
	s = s.replace(/<(u|ins)>([\s\S]*?)<\/\1>/gi, "$2");
	s = s.replace(/<(s|strike|del)>([\s\S]*?)<\/\1>/gi, "~~$2~~");
	s = s.replace(/<a\s+href=["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi, "[$2]($1)");
	// Streaming can temporarily cut a link at `<a href="https://...`; show the URL
	// instead of leaking raw, half-open HTML into the live answer.
	s = s.replace(/<a\s+href=["']?([^"'\s>]+)[^>\n]*/gi, "$1");
	s = s.replace(/<blockquote(?:\s+expandable)?[^>]*>([\s\S]*?)<\/blockquote>/gi, (_, body) => {
		const lines = decodeHtml(body).split(/\n+/).map((line) => `> ${line}`);
		return `\n${lines.join("\n")}\n`;
	});
	// Do not strip residual tags: markdown-it runs with html=false, so raw HTML/XML
	// is escaped safely. A broad tag strip would delete XML transcript/content tags.
	return protectedMarkdown.restore(protectedGeneratedCode.restore(decodeHtml(s)));
}

export function normalizeLegacyMathBlocks(text) {
	const protectedMarkdown = protectMarkdownCode(String(text || ""));
	const normalized = protectedMarkdown.text.replace(
		/(^|\n)[ \t]*\[[ \t]*\n([\s\S]*?)\n[ \t]*\][ \t]*(?=\n|$)/g,
		(_match, before, formula) => `${before}\\[\n${formula}\n\\]`,
	);
	return protectedMarkdown.restore(normalized);
}

export function renderMarkdownRaw(text, options = {}) {
	const renderer = options?.live || options?.highlight === false ? liveMd : md;
	return renderer.render(normalizeLegacyMathBlocks(richHtmlToMarkdown(text)));
}

export function renderMarkdown(text, options = {}) {
	const source = String(text || "");
	if (!source) return "";
	const live = Boolean(options?.live || options?.disableCache || options?.highlight === false);
	if (live || source.length > MARKDOWN_CACHE_MAX_SOURCE_CHARS) return renderMarkdownRaw(source, {live, highlight: live ? false : options?.highlight});
	const cached = markdownCache.get(source);
	if (cached !== undefined) return touchCache(markdownCache, source, cached, MARKDOWN_CACHE_LIMIT);
	return touchCache(markdownCache, source, renderMarkdownRaw(source, options), MARKDOWN_CACHE_LIMIT);
}

export function clearMarkdownCache() {
	markdownCache.clear();
	plainTextCache.clear();
}

export function plainText(text) {
	const source = String(text || "");
	if (!source) return "";
	if (source.length <= PLAIN_TEXT_CACHE_MAX_SOURCE_CHARS) {
		const cached = plainTextCache.get(source);
		if (cached !== undefined) return touchCache(plainTextCache, source, cached, PLAIN_TEXT_CACHE_LIMIT);
		return touchCache(plainTextCache, source, richHtmlToMarkdown(source).replace(/\s+/g, " ").trim(), PLAIN_TEXT_CACHE_LIMIT);
	}
	return richHtmlToMarkdown(source).replace(/\s+/g, " ").trim();
}

function isPlaceholderAnswerText(text) {
	const compact = plainText(text).replace(/\s+/g, "");
	return Boolean(compact) && /^[.…]+$/.test(compact);
}

export function hasMeaningfulAnswerText(text) {
	return Boolean(plainText(text).length) && !isPlaceholderAnswerText(text);
}

export function answerContent(text) {
	return hasMeaningfulAnswerText(text) ? String(text || "") : "";
}
