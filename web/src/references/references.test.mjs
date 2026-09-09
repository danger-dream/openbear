import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {referenceToken,referenceUrl,referenceFromUrl,normalizeReference,textToDocument,documentToText,referencesInText,parseReferenceText,referenceDisplayText} from './codec.js';
import {applyCatalogPacket,referenceCatalog,searchReferences,stopReferenceCatalog} from './catalog.js';
import {referenceChipOpen,referenceChipClose,escapeReferenceHtml} from './presentation.js';

const doc={kind:'doc',id:'17',label:'测试文档',scope:'full'};
const syntaxCases=JSON.parse(readFileSync(new URL('../../../tests/fixtures/reference_syntax.json',import.meta.url),'utf8'));
for(const {name,text,ids} of syntaxCases){
 test(`shared reference syntax: ${name}`,()=>{
  const parts=parseReferenceText(text);
  assert.deepEqual(parts.filter(part=>part.type==='reference').map(part=>part.attrs.id),ids);
  assert.equal(documentToText(textToDocument(text)),text,'draft, clipboard and restart preserve original source');
  if(!ids.length)assert.equal(referenceDisplayText(text),text,'literal display must not strip a reference-looking example');
 });
}
for(const label of ['测试文档','same name','中英 Mixed 名称','[方括号] (圆括号) *星号* _下划线_ `代码` \\','<script>alert(1)</script>','emoji 🦜']){
 test(`reference codec preserves identity and label: ${label}`,()=>{
  const ref={...doc,label};const text='前文 '+referenceToken(ref)+'\n后文';
  const document=textToDocument(text);
  assert.equal(document.content[0].content.filter(node=>node.type==='reference').length,1);
  assert.equal(documentToText(document),text);
  assert.deepEqual(referencesInText(text),[ref]);
 });
}
test('range and exact historical IDs survive edit/durable-copy/restart round trip',()=>{
 const refs=[{kind:'chat',id:'conversation-a',label:'会话',scope:'recent',turns:7},{kind:'turn',id:'conversation-a',itemId:'turn-a',label:'本轮',scope:'full'},{kind:'message',id:'conversation-a',itemId:'assistant:turn-a:0',label:'消息',scope:'full'}];
 const text=refs.map(referenceToken).join(' 和 ');assert.equal(documentToText(textToDocument(text)),text);assert.deepEqual(referencesInText(text),refs);
});
test('same titles never collapse different IDs; repeated same reference is deduplicated',()=>{
 const text=[doc,{...doc,id:'18'},{...doc,label:'alias'}].map(referenceToken).join(' ');
 assert.deepEqual(referencesInText(text).map(ref=>ref.id),['17','18']);
});
test('plain text and Markdown examples remain literal, including open code fences',()=>{
 for(const text of ['# 标题\n\n**普通 Markdown**\n','``'+referenceToken(doc)+'``','```\n'+referenceToken(doc)+'\n```','```\n'+referenceToken(doc),'`'+referenceToken(doc)+'`'])assert.equal(documentToText(textToDocument(text)),text);
 assert.equal(referencesInText('```\n'+referenceToken(doc)).length,0);
 assert.equal(documentToText(textToDocument('\\`literal\\`')), '\\`literal\\`');
});
test('untrusted schemes, kinds and out-of-range database IDs cannot become reference nodes',()=>{
 assert.equal(referenceFromUrl('javascript:alert(1)'),null);
 assert.equal(referenceFromUrl('https://example.invalid/doc/17'),null);
 assert.equal(normalizeReference({...doc,id:'999999999999999999999999'}),null);
 assert.equal(normalizeReference({...doc,id:'0'}),null);
 assert.equal(normalizeReference({...doc,kind:'system'}),null);
});
test('shared capsule presentation escapes labels and never embeds resource contents',()=>{
 const ref={...doc,label:'<img src=x onerror=alert(1)>'};const html=referenceChipOpen(ref)+escapeReferenceHtml(ref.label)+referenceChipClose(ref);
 assert.ok(html.includes('reference-chip--doc'));
 assert.ok(html.includes('&lt;img'));
 assert.ok(!html.includes('<img'));
 assert.ok(html.includes(referenceUrl(ref)));
});
function catalog(){stopReferenceCatalog({clear:true});applyCatalogPacket({type:'snapshot',epoch:'e',seq:1,items:[
 {key:'doc:1',kind:'doc',id:'1',name:'publish',label:'OpenBear 发布流程',group:'项目文档',updatedAt:1},
 {key:'doc:2',kind:'doc',id:'2',name:'build-guide',label:'OpenBear Build Guide',group:'项目文档',updatedAt:2},
 {key:'mem:3',kind:'mem',id:'3',name:'network',label:'网络配置',group:'运维',updatedAt:3},
 {key:'chat:current',kind:'chat',id:'current',name:'current',label:'当前会话',group:'临时会话'},
 ]});}
for(const [query,key] of [['发布','doc:1'],['FABU','doc:1'],['fblc','doc:1'],['openbearfblc','doc:1'],['openbear fabuliucheng','doc:1'],['BUILD','doc:2'],['buid','doc:2'],['wl配置','mem:3'],['wangluo','mem:3']]){
 test(`local search matches Chinese, English, pinyin or typo: ${query}`,()=>{catalog();assert.ok(searchReferences(query).some(item=>item.key===key));});
}
test('catalog patches update lookup immediately, discard replay and reject gaps',()=>{
 catalog();assert.equal(applyCatalogPacket({type:'patch',epoch:'e',seq:2,previousSeq:1,upserts:[{key:'doc:5',kind:'doc',id:'5',name:'new',label:'即时创建',group:''}],removed:['doc:1']}),true);
 assert.equal(searchReferences('jishichuangjian')[0].id,'5');assert.equal(searchReferences('fblc').length,0);
 assert.equal(applyCatalogPacket({type:'patch',epoch:'e',seq:2,previousSeq:1,upserts:[],removed:['doc:5']}),true);assert.ok(searchReferences('即时创建').length);
 assert.equal(applyCatalogPacket({type:'patch',epoch:'e',seq:4,previousSeq:3}),false);assert.equal(referenceCatalog.stale,true);
 applyCatalogPacket({type:'snapshot',epoch:'new',seq:7,items:[]});assert.equal(referenceCatalog.items.length,0);assert.equal(referenceCatalog.stale,false);
});
test('type and current-conversation filters do not hide unrelated valid matches',()=>{catalog();assert.equal(searchReferences('',{kind:'doc'}).length,2);assert.ok(!searchReferences('',{currentConversation:'current'}).some(item=>item.id==='current'));});
test('editor replacement is scoped to the original text area and does not use el-Popover',()=>{
 const composer=readFileSync(new URL('../views/consoleView/ConsoleComposer.vue',import.meta.url),'utf8');
 assert.ok(composer.includes('<ReferenceEditor'));
 assert.ok(composer.includes('class="attachment-strip"'));
 assert.ok(composer.includes('class="composer-toolbar"'));
 const floating=readFileSync(new URL('./FloatingPanel.vue',import.meta.url),'utf8');assert.ok(floating.includes('<Teleport'));assert.ok(!floating.includes('el-popover'));
 const editor=readFileSync(new URL('./ReferenceEditor.vue',import.meta.url),'utf8');assert.ok(editor.includes('VueNodeViewRenderer'));assert.ok(editor.includes("if(event.isComposing||view.composing"));assert.ok(editor.includes('@drop.capture'));
});
