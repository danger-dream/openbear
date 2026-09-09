import test from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {chromium} from 'playwright-core';
import {browserExecutable,componentBundle,webRoot} from '../../../test-support/realBrowser.mjs';

const executablePath=browserExecutable();
const code="function inspect() {\n  const sample = '<literal> & `ticks`';\n  const long = '"+'x'.repeat(2200)+"';\n  return sample;\n}\n";
const body='## 检查说明\n\n请复制 **下面的代码**，保留 `sample` 字段。\n\n- 只读检查\n- 不修改设置\n\n```js\n'+code+'```\n\n最后的说明\n\n[帮助](https://example.invalid)\n\n<script>window.__interactionExecuted=true</script>';
const questionDescription='问题 **说明** 与 `key`\n\n```js\n  nested();\n```';
const literalAnswer='  **这是原始回答**\n```js\n  answer();\n```  ';
const longBody=body+'\n\n'+Array.from({length:45},(_,i)=>`第 ${i+1} 段：保留正文可读性，长说明只在内容区域滚动，不挤压操作按钮。`).join('\n\n')+'\n\n| 名称 | 配置参数 | 测试方法 | 验证结果 |\n| --- | --- | --- | --- |\n| 移动端布局测试 | '+ 'long_setting_value_'.repeat(15)+' | 横向滚动查看完整配置，不逐字挤压 | 完整保留 |\n\n内容结束标记';

test('interaction Markdown renders in pending cards and history, copies exact code, and preserves answer semantics', {skip:!executablePath,timeout:60000},async t=>{
  const bundle=await componentBundle(`
    import {createApp,h,reactive,nextTick} from 'vue';
    import ElementPlus from 'element-plus';
    import ConsoleComposer from './src/views/consoleView/ConsoleComposer.vue';
    import ConsoleUserInteractionEvent from './src/views/consoleView/ConsoleUserInteractionEvent.vue';
    const state=reactive({action:'prompt',body:${JSON.stringify(body)},sensitive:false,serial:0,count:1,questions:null});
    const answers=[];let bubbledCopies=0;
    function item(){return {confirmationId:'fixture-'+state.serial,action:state.action,title:'交互 Markdown 验证',body:state.body,sensitive:state.sensitive,
      options:[{label:'选项 A',value:'a'}],questions:state.questions||[{id:'q',type:'open',question:'检查结果',description:${JSON.stringify(questionDescription)},required:true}]};}
    function history(){return {kind:'user_interaction',operation:{opType:'user_interaction',status:'completed',payload:{arguments:JSON.stringify(item()),result:JSON.stringify({status:'answered',confirmed:true,value:${JSON.stringify(literalAnswer)},text:${JSON.stringify(literalAnswer)},answers:[{questionId:'q',text:${JSON.stringify(literalAnswer)}}]})}}};}
    createApp({render:()=>h('main',{class:'fixture',onClick:event=>{if(event.target.closest('.md-code-copy'))bubbledCopies++;}},[
      h(ConsoleComposer,{key:state.serial,pendingConfirmations:Array.from({length:state.count},(_,i)=>({...item(),confirmationId:item().confirmationId+'-'+i})),onAnswerConfirmation:(...args)=>answers.push(args)}),
      h(ConsoleUserInteractionEvent,{event:history(),open:true})
    ])}).use(ElementPlus).mount('#app');
    window.interactionFixture={async set(action,body,sensitive=false,extra={}){state.action=action;state.body=body;state.sensitive=sensitive;state.count=extra.count||1;state.questions=extra.questions||null;state.serial++;answers.length=0;await nextTick();},answers:()=>answers,bubbled:()=>bubbledCopies};
  `);
  const css=(await Promise.all([
    'node_modules/element-plus/dist/index.css','src/dark-theme.css','src/views/consoleView/userInteractionTokens.css'
  ].map(name=>readFile(webRoot+'/'+name,'utf8')))).join('\n');
  const server=createServer((req,res)=>{
    if(req.url==='/bundle.js'){res.writeHead(200,{'Content-Type':'text/javascript'});return res.end(bundle);}
    if(req.url.endsWith('.css')){res.writeHead(200,{'Content-Type':'text/css'});return res.end(css);}
    res.writeHead(200,{'Content-Type':'text/html'});res.end('<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/style.css"><style>body{margin:0}.fixture{box-sizing:border-box;width:min(760px,100%);padding:12px;margin:auto}html.dark body{background:#18181b}</style></head><body><div id="app"></div><script src="/bundle.js"></script></body></html>');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  t.after(async()=>{await browser?.close();server.closeAllConnections();await new Promise(resolve=>server.close(resolve));});
  browser=await chromium.launch({executablePath,headless:true,args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1360,height:1000}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{
    window.__copied=[];window.__copyMode='modern';
    const clipboard={writeText:async value=>{if(window.__copyMode==='failure')throw Error('test denied');window.__copied.push({mode:'modern',value});}};
    Object.defineProperty(navigator,'clipboard',{configurable:true,get:()=>window.__copyMode==='legacy'?undefined:clipboard});
    const original=document.execCommand.bind(document);
    document.execCommand=(command,...args)=>{
      if(command!=='copy')return original(command,...args);
      if(window.__copyMode==='failure')return false;
      const value=document.activeElement?.value;
      const result=original(command,...args);if(result)window.__copied.push({mode:'legacy',value});return result;
    };
  });
  await page.goto(`http://127.0.0.1:${server.address().port}`);await page.waitForFunction(()=>window.interactionFixture);
  for(const action of ['prompt','confirm','select','questionnaire']){
    await page.evaluate(({action,body})=>window.interactionFixture.set(action,body),{action,body});
    const pending=page.locator('.web-confirm-card'),history=page.locator('.readonly-card');
    for(const card of [pending,history]){
      const intro=card.locator('.interaction-markdown').first();
      assert.equal(await intro.locator('h2').innerText(),'检查说明');
      assert.equal(await intro.locator('strong').innerText(),'下面的代码');
      assert.equal(await intro.locator('li').count(),2);
      assert.equal(await intro.locator('pre code').textContent(),code,'fences removed, complete source and indentation retained');
      assert.ok((await intro.innerText()).includes('最后的说明'),'history must not truncate at 2000 characters');
      assert.equal(await intro.locator('script').count(),0);
      assert.equal(await intro.locator('a').getAttribute('target'),'_blank');
      await intro.locator('.md-code-copy').click();
      assert.equal((await page.evaluate(()=>window.__copied.at(-1))).value,code);
      assert.equal(await intro.locator('.md-code-copy').innerText(),'已复制');
    }
    assert.equal(await page.evaluate(()=>window.interactionFixture.bubbled()),0,'copy is handled once, not by outer timeline');
    assert.deepEqual(await page.evaluate(()=>window.interactionFixture.answers()),[],'copy must never submit/confirm');
    assert.equal(await page.evaluate(()=>window.__interactionExecuted),undefined,'body code and raw HTML are not executed');
    if(action==='questionnaire'){
      for(const card of [pending,history]){
        assert.equal(await card.locator('.question-description strong').innerText(),'说明');
        assert.equal(await card.locator('.question-description pre code').textContent(),'  nested();\n');
      }
    }
    const input=pending.locator(action==='questionnaire'?'.question-free-text textarea':'.web-interaction-input textarea');
    await input.fill(literalAnswer);
    await pending.locator('.web-confirm-btn.confirm').click();
    const answers=await page.evaluate(()=>window.interactionFixture.answers());assert.equal(answers.length,1);
    const result=answers[0][1];
    assert.equal(action==='prompt'?result.value:action==='questionnaire'?result.answers[0].text:result.text,literalAnswer);
    if(action==='confirm'){assert.equal(result.confirmed,false);assert.equal(result.decision,'feedback');}
  }
  await page.evaluate(body=>window.interactionFixture.set('prompt',body),body);
  await page.evaluate(()=>window.__copyMode='legacy');
  await page.locator('.web-confirm-card .md-code-copy').click();
  const copied=await page.evaluate(()=>window.__copied.at(-1));assert.equal(copied.mode,'legacy');assert.equal(copied.value,code);
  await page.evaluate(()=>window.__copyMode='failure');
  await page.locator('.web-confirm-card .md-code-copy').click();
  assert.equal(await page.locator('.web-confirm-card .md-code-copy').innerText(),'复制失败');
  assert.deepEqual(await page.evaluate(()=>window.interactionFixture.answers()),[]);
  for(const width of [1360,390]){
    await page.setViewportSize({width,height:900});
    for(const dark of [false,true]){
      await page.evaluate(dark=>document.documentElement.classList.toggle('dark',dark),dark);
      for(const action of ['prompt','questionnaire']){
        await page.evaluate(({action,body})=>window.interactionFixture.set(action,body),{action,body});
        const metrics=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,blocks:[...document.querySelectorAll('.interaction-markdown pre')].map(el=>({width:el.clientWidth,scroll:el.scrollWidth,whiteSpace:getComputedStyle(el).whiteSpace}))}));
        if(metrics.scroll>metrics.width+1)console.error('OVERFLOW',JSON.stringify(await page.evaluate(()=>[...document.querySelectorAll('.composer-shell,.composer-content,.web-confirm-stack,.web-confirm-card,.interaction-markdown,.interaction-detail,.readonly-card')].map(el=>({class:el.className,width:el.getBoundingClientRect().width,scroll:el.scrollWidth,min:getComputedStyle(el).minWidth})))));
        assert.ok(metrics.scroll<=metrics.width+1,`${action}/${dark}/${width}: no page-wide overflow ${JSON.stringify(metrics)}`);
        assert.ok(metrics.blocks.every(block=>block.width<=width&&block.whiteSpace==='pre'));
      }
    }
  }
  for(const viewport of [{width:1360,height:900},{width:390,height:740},{width:360,height:430}]){
    await page.setViewportSize(viewport);
    for(const dark of [false,true]){
      await page.evaluate(dark=>document.documentElement.classList.toggle('dark',dark),dark);
      for(const action of ['prompt','confirm','select','questionnaire']){
        await page.evaluate(({action,body})=>window.interactionFixture.set(action,body,false,action==='questionnaire'?{questions:[{id:'q',type:'open',question:'检查结果',description:body,required:true}]}:{}),{action,body:longBody});
        assert.equal(await page.locator('.web-interaction-content textarea,.web-interaction-content input').count(),0,'answer controls must never be inside the description scroll area');
        assert.equal(await page.locator('.readonly-content .readonly-text-answer,.readonly-content .readonly-options').count(),0,'saved answers must stay outside the description scroll area');
        const historyHeight=await page.locator('.readonly-card').evaluate(el=>el.getBoundingClientRect().height);
        assert.ok(historyHeight<viewport.height-20,`expanded history is bounded: ${historyHeight}/${viewport.height}`);
        const pendingHeight=await page.locator('.web-confirm-card').evaluate(el=>el.getBoundingClientRect().height);
        assert.ok(pendingHeight<viewport.height-20,`${action}: pending card fits the viewport: ${pendingHeight}/${viewport.height}; ${JSON.stringify(await page.locator('.web-confirm-card').evaluate(el=>[...el.querySelectorAll(':scope > *, .question-description, .question-free-text, .question-hint')].map(e=>({class:e.className,height:e.getBoundingClientRect().height}))))}`);
        await page.locator('.web-confirm-card').evaluate(el=>window.scrollTo(0,el.getBoundingClientRect().top+scrollY-12));
        const answer=page.locator('.web-confirm-card textarea').first();
        const answerBefore=await answer.boundingBox();
        assert.ok(await answer.evaluate(el=>{const r=el.getBoundingClientRect();return [0.15,0.85].every(y=>el.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height*y)));}),'answer is directly reachable before scrolling any description');
        assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'long content never widens the page');
        for(const [contentSelector,headerSelector,actionsSelector] of [
          ['.web-interaction-content','.web-confirm-title','.web-confirm-actions'],
          ['.readonly-content','.readonly-title',null]
        ]){
          const content=page.locator(contentSelector);
          const before=await page.locator(headerSelector).boundingBox();
          const actionsBefore=actionsSelector?await page.locator(actionsSelector).boundingBox():null;
          const metrics=await content.evaluate(el=>({height:el.clientHeight,scroll:el.scrollHeight,overflow:getComputedStyle(el).overflowY}));
          assert.ok(metrics.height>24&&metrics.height<=viewport.height*.24+5&&metrics.scroll>metrics.height&&metrics.overflow==='auto',`${contentSelector}: only explanation has bounded internal scrolling`);
          await content.evaluate(el=>el.scrollTop=el.scrollHeight);
          assert.ok(await content.evaluate(el=>el.scrollTop>0),'scroll can reach the bottom');
          const after=await page.locator(headerSelector).boundingBox();
          assert.equal(after.y,before.y,'heading stays outside scrolling content');
          if(actionsSelector){
            assert.deepEqual(await page.locator(actionsSelector).boundingBox(),actionsBefore,'actions stay outside scrolling content');
            assert.deepEqual(await answer.boundingBox(),answerBefore,'answer stays fixed while the description scrolls');
            if(action==='questionnaire'){
              assert.ok(await page.locator('.web-confirm-card .question-description').evaluate(el=>{el.scrollTop=el.scrollHeight;return el.scrollTop>0;}),'long question explanation scrolls on its own');
              assert.deepEqual(await answer.boundingBox(),answerBefore,'question answer is independent of its own long explanation too');
            }
          }
          await content.evaluate(el=>el.scrollTop=0);
          for(const selector of ['pre','.md-table-scroll']){
            const wide=content.locator(selector).first();
            assert.ok(await wide.evaluate(el=>{el.scrollLeft=160;return el.scrollWidth>el.clientWidth&&el.scrollLeft>0;}),`${selector}: independent horizontal scrolling`);
          }
        }
      }
    }
  }
  await page.evaluate(body=>window.interactionFixture.set('prompt',body,true),body);
  assert.equal(await page.locator('.readonly-card .interaction-markdown').count(),0,'sensitive history body remains hidden');
  assert.equal(await page.locator('.readonly-card .redacted-answer').count(),1);
  await page.evaluate(()=>window.interactionFixture.set('prompt','第一行\n第二行'));
  assert.equal(await page.locator('.web-confirm-body br').count(),1,'plain text line breaks remain visible');
  await page.setViewportSize({width:1360,height:900});
  assert.ok(await page.locator('.readonly-content').evaluate(el=>el.clientHeight<180&&el.scrollHeight===el.clientHeight),'short content keeps its natural height');
  await page.setViewportSize({width:390,height:740});
  await page.evaluate(()=>window.interactionFixture.set('prompt','简短说明',false,{count:3}));
  assert.ok(await page.locator('.web-confirm-stack').evaluate(el=>{el.scrollTop=el.scrollHeight;return el.clientHeight<=innerHeight*.58+1&&el.scrollTop>0;}),'multiple pending cards share a bounded stack');
  await page.locator('.web-confirm-card').last().locator('textarea').fill('最后一张卡片');
  await page.locator('.web-confirm-card').last().locator('.web-confirm-btn.confirm').click();
  assert.equal((await page.evaluate(()=>window.interactionFixture.answers()))[0][1].value,'最后一张卡片','stacked cards remain reachable and answerable');
  await page.evaluate(()=>window.interactionFixture.set('questionnaire','填写最后一题',false,{questions:Array.from({length:12},(_,i)=>({id:'q'+i,type:'open',question:'第 '+(i+1)+' 题',required:i===11}))}));
  await page.locator('.web-confirm-btn.confirm').click();
  assert.ok(await page.locator('.question-free-text textarea').last().evaluate(el=>el===document.activeElement),'validation focuses the last required question inside the scroll area');
  assert.ok(await page.locator('.web-confirm-card .questionnaire-questions').evaluate(el=>el.scrollTop>0),'validation reveals the offscreen question without scrolling the introduction');
  await page.locator('.question-free-text textarea').last().fill(literalAnswer);
  await page.locator('.web-confirm-btn.confirm').click();
  assert.equal((await page.evaluate(()=>window.interactionFixture.answers()))[0][1].answers.at(-1).text,literalAnswer,'long questionnaire still submits the original answer');
  assert.deepEqual(errors,[]);
});
