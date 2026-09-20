// Node-only regression harness: import the actual renderer (CSS has no runtime
// meaning here). No browser, provider requests, DOM layout or service is used.
import {register, createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import {parseReferenceText, referenceKey} from './codec.js';

register(`data:text/javascript,${encodeURIComponent(`
 export function load(url, context, nextLoad) {
  if (url.endsWith('.css')) return {format:'module', source:'export default {};', shortCircuit:true};
  return nextLoad(url, context);
 }
`)}`, {parentURL: import.meta.url});
const require = createRequire(import.meta.url);
const {decodeHTML} = createRequire(require.resolve('markdown-it'))('entities');
// textarea decoding uses HTML entities, not Markdown backslash unescaping.
globalThis.document = {createElement(tag) {
 if (tag !== 'textarea') throw new Error(`Unexpected DOM access: ${tag}`);
 return {value:'', set innerHTML(value) {this.value = decodeHTML(value);}};
}};
export const {renderMarkdown, renderMarkdownRaw} = await import('../views/consoleView/markdown.js');
export function renderCase({text, references}) {
 const parsed = parseReferenceText(text).filter(part => part.type === 'reference').map(part => part.attrs);
 return {parsed, keys: parsed.map(referenceKey), html: renderMarkdown(text), frozenHtml: renderMarkdown(text, {references})};
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
 console.log(JSON.stringify(JSON.parse(readFileSync(0, 'utf8')).map(renderCase)));
}
