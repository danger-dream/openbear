// Applied before any artifact markup. The iframe must additionally use sandbox="allow-scripts"
// (never allow-same-origin); HTML scripts/styles stay inside an opaque-origin document.
const PREVIEW_CSP = [
	"default-src 'none'",
	"script-src 'unsafe-inline'",
	"style-src 'unsafe-inline'",
	"img-src data: blob:",
	"font-src data:",
	"media-src data: blob:",
	"connect-src 'none'",
	"frame-src 'none'",
	"object-src 'none'",
	"base-uri 'none'",
	"form-action 'none'",
].join("; ");

export function htmlPreviewDocument(source) {
	// Only the rendered copy is wrapped; source view and download retain the exact file.
	return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${PREVIEW_CSP}"><meta name="referrer" content="no-referrer">${String(source || "")}`;
}
