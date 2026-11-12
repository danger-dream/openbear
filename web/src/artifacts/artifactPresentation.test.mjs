import test from "node:test";
import assert from "node:assert/strict";
import {artifactFormat} from "./artifactFiles.js";
import {artifactPresentation} from "./artifactPresentation.js";

const families = {
	html: ["html", "htm"], image: ["png", "jpg", "jpeg", "webp", "gif", "avif", "bmp", "ico"],
	markdown: ["md", "markdown"], pdf: ["pdf"], document: ["doc", "docx", "docm", "odt", "rtf", "pages", "wps"],
	spreadsheet: ["xls", "xlsx", "xlsm", "ods", "csv", "tsv", "numbers"], presentation: ["ppt", "pptx", "ppsx", "odp", "key"],
	archive: ["zip", "7z", "rar", "tar", "gz", "tgz", "bz2", "xz", "zst"], audio: ["mp3", "wav", "flac", "m4a", "aac", "ogg", "opus"],
	video: ["mp4", "webm", "mov", "avi", "mkv"], font: ["ttf", "otf", "woff", "woff2"],
	code: ["js", "ts", "py", "go", "rs", "java", "sh", "css", "svg"], data: ["json", "jsonl", "yaml", "yml", "toml", "xml"], text: ["txt", "log", "rst"], file: ["bin", "unknown"]
};
for (const [category, extensions] of Object.entries(families)) test(`common file family: ${category}`, () => {
	for (const ext of extensions) {
		const meta = {fileName: `文件.${ext.toUpperCase()}`, mimeType: "application/octet-stream"};
		const presentation = artifactPresentation(meta);
		assert.equal(presentation.category, category, ext);
		assert.ok(presentation.badge.length > 0);
		assert.equal(presentation.previewable, ["html", "image", "audio", "video", "markdown", "text", "code"].includes(artifactFormat(meta).kind), ext);
	}
});

test("extensionless files can use MIME and compound archives retain their actual type", () => {
	for (const [mimeType, category] of [["application/pdf", "pdf"], ["application/msword", "document"], ["application/vnd.openxmlformats-officedocument.wordprocessingml.document", "document"], ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "spreadsheet"], ["application/vnd.ms-powerpoint", "presentation"], ["application/zip", "archive"], ["audio/mpeg", "audio"], ["video/mp4", "video"], ["font/woff2", "font"], ["text/markdown", "markdown"], ["text/html", "html"]]) {
		assert.equal(artifactPresentation({fileName: "附件", mimeType: `${mimeType}; charset=utf-8`}).category, category, mimeType);
	}
	assert.equal(artifactPresentation({fileName: "archive.tar.gz"}).badge, "TAR.GZ");
});

test("presentation never advertises nonexistent PDF, Office or media preview", () => {
	for (const ext of ["pdf", "docx", "xlsx", "pptx", "zip", "mkv", "avi", "wmv", "woff2", "bin"]) {
		const p = artifactPresentation({fileName: `file.${ext}`});
		assert.equal(p.previewable, false, ext); assert.equal(p.action, "下载查看", ext);
	}
	assert.equal(artifactPresentation({fileName: "a.csv", mimeType: "text/csv"}).action, "查看文本");
	assert.equal(artifactPresentation({fileName: "a.md"}).action, "阅读文档");
});

test("browser-playable audio and video are offered for playback, keeping their own category", () => {
	for (const [meta, category, action] of [[{fileName: "a.mp3"}, "audio", "播放音频"], [{fileName: "录音.m4a", mimeType: "audio/mp4"}, "audio", "播放音频"], [{fileName: "voice.webm", mimeType: "audio/webm"}, "audio", "播放音频"], [{fileName: "clip.mp4", mimeType: "video/mp4"}, "video", "播放视频"], [{fileName: "IMG_0001.MOV", mimeType: "video/quicktime"}, "video", "播放视频"]]) {
		const p = artifactPresentation(meta);
		assert.deepEqual([p.category, p.previewable, p.action], [category, true, action], meta.fileName);
	}
	assert.equal(artifactPresentation({fileName: "movie.mkv", mimeType: "video/x-matroska"}).category, "video");
});

test("active HTML/SVG cannot become thumbnail images due to their label or MIME", () => {
	for (const meta of [{fileName: "a.html", mimeType: "image/png"}, {fileName: "a.svg", mimeType: "image/png"}, {fileName: "a.png", mimeType: "image/svg+xml"}, {fileName: "a.png", mimeType: "text/html"}]) {
		assert.notEqual(artifactPresentation(meta).category, "image");
	}
	assert.equal(artifactPresentation({fileName: "a.svg"}).action, "查看源码");
	assert.equal(artifactPresentation(null).label, "附件");
	assert.equal(artifactPresentation().action, "查看附件");
});
