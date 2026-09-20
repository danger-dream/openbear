import test from 'node:test';
import assert from 'node:assert/strict';
import {Schema} from '@tiptap/pm/model';
import {TextSelection} from '@tiptap/pm/state';
import {mentionAtPosition} from './mention.js';
import {textToDocument,normalizeReference,referenceToken} from './codec.js';
import {referenceCandidateDetail} from './presentation.js';

const schema = new Schema({nodes:{
  doc:{content:'paragraph+'}, paragraph:{content:'inline*'}, text:{group:'inline'},
  hardBreak:{group:'inline',inline:true},
  reference:{group:'inline',inline:true,atom:true,attrs:{kind:{},id:{},label:{},scope:{default:'full'}}}
}});
function position(text) {const doc=schema.nodeFromJSON(textToDocument(text));return TextSelection.atEnd(doc).$from;}
for (const before of ['', ' ', '上一行没有空格\n', 'first line\n\n', '看下', '看看：', 'test(', 'test,', '🦜', '[文档](openbear://ref/doc/17)']) {
  test(`mention recognizes actual editor boundary: ${JSON.stringify(before)}`, () => {
    const pos=position(before+'@doc/发布');
    const match=mentionAtPosition(pos);
    assert.deepEqual(match,{from:pos.pos-'@doc/发布'.length,to:pos.pos,query:'发布',kind:'doc'});
    // The replacement range contains @ + query only, never preceding text or break.
    assert.equal(pos.doc.textBetween(match.from,match.to),'@doc/发布');
  });
}
for (const text of ['mail@example.com','user.name+tag@example.com','hello@','https://host/@name','/path/@name','```\n@doc/发布','```js\nline\n@doc/发布']) {
  test(`mention preserves email/path/code suppression: ${JSON.stringify(text)}`,()=>assert.equal(mentionAtPosition(position(text)),null));
}
test('mention works after a closed code block and excludes earlier @ on other lines',()=>{
  const text='```\n@inside\n```\n@outside';
  assert.equal(mentionAtPosition(position(text)).query,'outside');
});
test('native multi-paragraph input preserves newline trigger and code-fence suppression',()=>{
  const paragraphs=lines=>TextSelection.atEnd(schema.nodeFromJSON({type:'doc',content:lines.map(text=>({type:'paragraph',content:[{type:'text',text}]}))})).$from;
  assert.equal(mentionAtPosition(paragraphs(['first line','@doc/发布'])).query,'发布');
  assert.equal(mentionAtPosition(paragraphs(['```','@doc/发布'])),null);
  assert.equal(mentionAtPosition(paragraphs(['```','code','```','@doc/发布'])).query,'发布');
});
for (const tail of [' ', '\n', "'", '"', '`', '。', '，', '.', ',', '!', '?', '：', ')', ' 这份文档里添加内容']) {
 test(`prose boundary ends completion: ${JSON.stringify(tail)}`,()=>assert.equal(mentionAtPosition(position('@mem/name'+tail)),null));
}
for (const text of ['`@doc/name', '``example ` @doc/name', '~~~js\n@doc/name', '  ````js\n```\n@doc/name', '"@mem/name', "'@mem/name", '“@mem/name']) {
 test(`quoted literal or code does not trigger: ${JSON.stringify(text)}`,()=>assert.equal(mentionAtPosition(position(text)),null));
}
test('wildcard and numeric searches remain valid; a new @ after punctuation opens normally',()=>{
 assert.equal(mentionAtPosition(position('@dkg*ss')).query,'dkg*ss');
 assert.equal(mentionAtPosition(position('@chat/2026')).query,'2026');
 assert.equal(mentionAtPosition(position('我在 @doc/name。再看 @mem/wl')).query,'wl');
 assert.equal(mentionAtPosition(position('`code` @doc/发布')).query,'发布');
});
test('candidate detail uses excerpts or key names, never credential value/notes',()=>{
  assert.equal(referenceCandidateDetail({kind:'mem',preview:'记忆正文约三十个字…'}),'记忆正文约三十个字…');
  assert.equal(referenceCandidateDetail({kind:'doc',preview:'文档正文',archived:true}),'文档正文 · 已归档');
  assert.equal(referenceCandidateDetail({kind:'secret',fieldKeys:['host','password'],value:'PRIVATE',note:'PRIVATE',preview:'PRIVATE',kv:[{key:'PRIVATE',value:'PRIVATE'}]}),'字段：host · password');
  assert.equal(referenceCandidateDetail({kind:'secret',fieldKeys:[]}), '暂无字段');
  assert.equal(referenceCandidateDetail({kind:'doc',preview:''}), '暂无正文');
  assert.equal(referenceCandidateDetail({kind:'doc'}), '摘要待同步');
});
test('preview metadata is not serialized into drag/copy/draft reference identifiers',()=>{
  const ref={kind:'secret',id:'17',label:'环境凭证',fieldKeys:['password'],preview:'not-copied'};
  assert.deepEqual(normalizeReference(ref),{kind:'secret',id:'17',label:'环境凭证',scope:'full'});
  assert.equal(referenceToken(ref),'[环境凭证](openbear://ref/secret/17)');
});
