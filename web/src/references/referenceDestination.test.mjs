import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {computed,ref} from 'vue';
import {renderCase,renderMarkdown} from './rendererHarness.mjs';
import {parseReferenceText,referenceKey,documentToText,textToDocument} from './codec.js';

const token = query => `[Credential](openbear://ref/secret/17?${query})`;
const variants = [
 ['mode=mention','mention'],
 ['scope=full&amp;mode=mention','mention'],
 ['scope=full&#38;mode=mention','mention'],
 ['scope=full&#x26;mode=mention','mention'],
 [String.raw`scope=full\&mode=mention`,'mention'],
 ['mode=&#109;ention','mention'],
 ['scope=full&amp;amp;mode=mention','content'],
 ['scope=full&amp;mode=content','content'],
];
for (const [query,mode] of variants) test(`destination decoded once in codec and real renderer: ${query}`,()=>{
 const text=token(query), result=renderCase({text});
 assert.equal(result.parsed.length,1);
 assert.equal(result.parsed[0].mode||'content',mode);
 assert.equal(result.html.includes('仅提及'),mode==='mention');
 assert.equal(result.html.includes('发送时将向当前模型提供凭证'),mode==='content');
 assert.deepEqual(parseReferenceText(documentToText(textToDocument(text))).filter(p=>p.type==='reference').map(p=>p.attrs),result.parsed);
});
test('decoded duplicate/unknown modes never produce a content capsule',()=>{
 for(const query of ['mode=mention&amp;mode=content',String.raw`mode=mention\&mode=content`,'mode=&#117;nknown']){
  const result=renderCase({text:token(query)});
  assert.deepEqual(result.parsed,[]);
  assert.doesNotMatch(result.html,/class="reference-chip /);
 }
});
test('entity/escape normalization does not activate literal reference examples',()=>{
 const source=token('scope=full&amp;mode=mention');
 for(const text of ['`'+source+'`','~~~\n'+source+'\n~~~','    '+source,'\\'+source]){
  const result=renderCase({text});assert.deepEqual(result.parsed,[]);assert.doesNotMatch(result.html,/class="reference-chip /);
  assert.equal(documentToText(textToDocument(text)),text);
 }
});
test('frozen per-occurrence modes override legacy interpretations and do not contaminate source cache',()=>{
 const text=token('scope=full&amp;mode=mention');
 const item={kind:'secret',id:'17',label:'Credential',scope:'full'};
 const content={...item,key:referenceKey(item),bundleId:'legacy-content'};
 const mention={...item,mode:'mention',key:referenceKey({...item,mode:'mention'}),bundleId:'mention'};
 assert.match(renderMarkdown(text),/仅提及/); // prime source-only cache
 const bound=renderMarkdown(text,{references:[content]});
 assert.doesNotMatch(bound,/仅提及/);assert.match(bound,/发送时将向当前模型提供凭证/);
 assert.match(renderMarkdown(text,{references:[mention]}),/仅提及/);
 assert.match(renderMarkdown(text),/仅提及/);
 const repeated=renderMarkdown(text+' '+text,{references:[content,mention]});
 assert.equal((repeated.match(/class="reference-chip__scope">仅提及/g)||[]).length,1);
 assert.equal((repeated.match(/发送时将向当前模型提供凭证/g)||[]).length,1);
 // Execute the actual ConsoleMarkdown computed wiring, not a parallel renderer call.
 const source=readFileSync(new URL('../views/consoleView/ConsoleMarkdown.vue',import.meta.url),'utf8');
 const wiring=source.slice(source.indexOf('const html = computed('),source.indexOf('\nfunction onMarkdownClick'));
 const html=vm.runInNewContext(wiring+'\nhtml.value',{computed,renderMarkdown,displayedText:ref(text),props:{live:false,references:[content]},appendLiveCaret:x=>x});
 assert.equal(html,bound);
});
