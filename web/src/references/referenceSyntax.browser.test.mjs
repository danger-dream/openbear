import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {chromium} from 'playwright-core';
import {browserExecutable,componentBundle} from '../../test-support/realBrowser.mjs';

const executablePath=browserExecutable();
test('real reference editor preserves literal paste and typed examples, while explicit capsules survive restore', {skip:!executablePath,timeout:60000},async t=>{
  const bundle=await componentBundle(`
    import {createApp,h,reactive,nextTick} from 'vue';
    import ReferenceEditor from './src/references/ReferenceEditor.vue';
    const state=reactive({text:'',sent:null});let editor;
    createApp({render:()=>h(ReferenceEditor,{ref:value=>editor=value,modelValue:state.text,'onUpdate:modelValue':value=>state.text=value,onSend:()=>state.sent=state.text})}).mount('#app');
    window.referenceFixture={
      async set(text){state.text=text;state.sent=null;await nextTick();},
      get(){return {text:state.text,sent:state.sent,doc:editor.editor.getJSON()};},
      insert(){editor.insertReference({kind:'doc',id:'17',label:'测试文档',scope:'full'});}
    };
  `);
  const previews=[];
  const server=createServer((req,res)=>{
    if(req.url==='/bundle.js'){res.writeHead(200,{'Content-Type':'text/javascript'});return res.end(bundle);}
    if(req.url==='/api/references/preview'){
      let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{
        previews.push(JSON.parse(body));res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({ok:true,items:[],estimatedTokens:0}));
      });return;
    }
    res.writeHead(200,{'Content-Type':'text/html'});res.end('<!doctype html><html><body><div id="app"></div><script src="/bundle.js"></script></body></html>');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  t.after(async()=>{await browser?.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));});
  browser=await chromium.launch({executablePath,headless:true,args:['--no-sandbox']});
  const page=await browser.newPage();const pageErrors=[];page.on('pageerror',error=>pageErrors.push(error.message));
  await page.goto(`http://127.0.0.1:${server.address().port}`);
  await page.waitForFunction(()=>window.referenceFixture);
  const link='[演示](openbear://ref/secret/999999)';
  const examples=['~~~text\n'+link+'\n~~~','    '+link,'\\'+link,'````\n```\n'+link+'\n````'];
  function refs(snapshot){return snapshot.doc.content.flatMap(node=>node.content||[]).filter(node=>node.type==='reference');}
  for(const text of examples){
    await page.evaluate(()=>window.referenceFixture.set(''));
    await page.locator('[contenteditable="true"]').focus();
    await page.evaluate(text=>{
      const data=new DataTransfer();data.setData('text/plain',text);
      document.querySelector('[contenteditable="true"]').dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));
    },text);
    let snapshot=await page.evaluate(()=>window.referenceFixture.get());
    assert.equal(snapshot.text,text);assert.deepEqual(refs(snapshot),[]);
    await page.keyboard.press('Enter');snapshot=await page.evaluate(()=>window.referenceFixture.get());
    assert.equal(snapshot.sent,text,'send preserves the literal source');
    await page.evaluate(()=>window.referenceFixture.set(''));
    await page.evaluate(text=>window.referenceFixture.set(text),text);
    snapshot=await page.evaluate(()=>window.referenceFixture.get());
    assert.equal(snapshot.text,text);assert.deepEqual(refs(snapshot),[],'draft/restart restore stays literal');
  }
  await page.evaluate(()=>window.referenceFixture.set(''));
  await page.locator('[contenteditable="true"]').focus();
  await page.keyboard.insertText('\\'+link);
  assert.equal((await page.evaluate(()=>window.referenceFixture.get())).text,'\\'+link);
  assert.equal(previews.length,0,'literal examples never trigger resource preview');
  await page.evaluate(()=>window.referenceFixture.set(''));
  const requested=page.waitForRequest(req=>req.url().endsWith('/api/references/preview'));
  await page.evaluate(()=>window.referenceFixture.insert());
  await requested;
  const original=await page.evaluate(()=>window.referenceFixture.get());
  assert.equal(refs(original).length,1);assert.equal(refs(original)[0].attrs.id,'17');
  assert.equal(original.text,'[测试文档](openbear://ref/doc/17) ');
  assert.ok(!JSON.stringify(previews).includes('reference-candidate-'),'parser tags are never sent over the network');
  await page.evaluate(()=>window.referenceFixture.set(''));
  await page.evaluate(text=>window.referenceFixture.set(text),original.text);
  assert.deepEqual(refs(await page.evaluate(()=>window.referenceFixture.get())),refs(original));
  assert.deepEqual(pageErrors,[]);
});
