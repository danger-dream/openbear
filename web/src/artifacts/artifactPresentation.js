import {artifactFormat} from "./artifactFiles.js";

// Visual families are independent of preview support. Recognizing a PDF/Office
// file must not send it to a text/image renderer or promise a viewer we lack.
const FAMILIES = [
	{category: "pdf", label: "PDF 文档", badge: "PDF", extensions: ["pdf"], mimes: ["application/pdf"]},
	{category: "document", label: "文档", badge: "DOC", extensions: ["doc", "docx", "docm", "dot", "dotx", "odt", "rtf", "pages", "wps"], mimes: ["application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/vnd.ms-word.document.macroenabled.12", "application/vnd.oasis.opendocument.text", "application/rtf", "text/rtf"]},
	{category: "spreadsheet", label: "表格", badge: "XLS", extensions: ["xls", "xlsx", "xlsm", "xlsb", "xlt", "xltx", "ods", "numbers", "et", "csv", "tsv"], mimes: ["application/vnd.ms-excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.ms-excel.sheet.macroenabled.12", "application/vnd.oasis.opendocument.spreadsheet", "text/csv", "text/tab-separated-values"]},
	{category: "presentation", label: "演示文稿", badge: "PPT", extensions: ["ppt", "pptx", "pptm", "pps", "ppsx", "odp", "key", "dps"], mimes: ["application/vnd.ms-powerpoint", "application/vnd.openxmlformats-officedocument.presentationml.presentation", "application/vnd.ms-powerpoint.presentation.macroenabled.12", "application/vnd.oasis.opendocument.presentation"]},
	{category: "archive", label: "压缩包", badge: "ZIP", extensions: ["zip", "7z", "rar", "tar", "gz", "tgz", "bz2", "xz", "zst", "zstd", "cab"], mimes: ["application/zip", "application/x-zip-compressed", "application/x-7z-compressed", "application/vnd.rar", "application/x-rar-compressed", "application/x-tar", "application/gzip", "application/x-gzip", "application/x-bzip2", "application/x-xz", "application/zstd"]},
	{category: "audio", label: "音频", badge: "AUDIO", extensions: ["mp3", "wav", "flac", "m4a", "aac", "ogg", "opus", "aiff", "wma"], prefix: "audio/"},
	{category: "video", label: "视频", badge: "VIDEO", extensions: ["mp4", "webm", "mov", "m4v", "avi", "mkv", "wmv", "ogv", "mpeg", "mpg", "3gp"], prefix: "video/"},
	{category: "font", label: "字体", badge: "FONT", extensions: ["ttf", "otf", "woff", "woff2"], prefix: "font/", mimes: ["application/font-woff", "application/vnd.ms-opentype", "application/x-font-ttf"]},
];

export function artifactPresentation(metadata) {
	const format = artifactFormat(metadata);
	const name = String(metadata?.fileName || "").toLowerCase();
	const ext = name.includes(".") ? name.split(".").pop() : "";
	const mime = String(metadata?.mimeType || "").split(";")[0].trim().toLowerCase();
	const extension = ext && ext.length <= 8 ? ext.toUpperCase() : "";
	let category = "file", label = "文件", badge = extension || "FILE";
	if (format.kind === "html") { category = "html"; label = "HTML 页面"; badge = "HTML"; }
	else if (format.kind === "image") { category = "image"; label = "图片"; badge = extension || "IMAGE"; }
	else if (format.kind === "audio") { category = "audio"; label = "音频"; badge = extension || "AUDIO"; }
	else if (format.kind === "video") { category = "video"; label = "视频"; badge = extension || "VIDEO"; }
	else if (format.kind === "markdown") { category = "markdown"; label = "Markdown"; badge = "MD"; }
	else if (format.sourceOnly) { category = "code"; label = "SVG 源码"; badge = "SVG"; }
	else {
		const family = FAMILIES.find(item => item.extensions.includes(ext) || item.mimes?.includes(mime) || item.prefix && mime.startsWith(item.prefix));
		if (family) {
			({category, label} = family); badge = extension || family.badge;
			if (category === "archive" && /\.tar\.(gz|bz2|xz|zst)$/.test(name)) badge = `TAR.${extension}`;
		} else if (format.kind === "code") {
			category = ["json", "yaml", "ini", "xml"].includes(format.language) ? "data" : "code";
			label = category === "data" ? "数据 / 配置" : "代码"; badge = format.label;
		} else if (format.kind === "text") { category = "text"; label = "文本"; badge = extension || "TXT"; }
	}
	const previewable = ["html", "image", "audio", "video", "markdown", "text", "code"].includes(format.kind);
	const action = !metadata ? "查看附件" : format.kind === "image" ? "查看大图" : format.kind === "audio" ? "播放音频" : format.kind === "video" ? "播放视频" : format.kind === "html" ? "页面 / 源码" : format.kind === "markdown" ? "阅读文档" : format.kind === "code" ? "查看源码" : format.kind === "text" ? "查看文本" : "下载查看";
	return {category, label: metadata ? label : "附件", badge: metadata ? badge : "FILE", action, previewable};
}
