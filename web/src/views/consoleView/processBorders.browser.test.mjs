import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {browserExecutable} from '../../../test-support/realBrowser.mjs';
import {parse,compileStyle} from '@vue/compiler-sfc';
import {chromium} from 'playwright-core';
const executablePath=browserExecutable();
function styles(name,id){
 const filename=new URL(name,import.meta.url).pathname;
 const {descriptor}=parse(readFileSync(filename,'utf8'),{filename});
 return descriptor.styles.map(style=>compileStyle({source:style.content,filename,id,scoped:style.scoped}).code).join('\n');
}

test('status rows and tool annotations have no dark outer borders while detail controls keep theirs',{skip:!executablePath,timeout:30000},async()=>{
 const browser=await chromium.launch({executablePath,headless:true,args:['--no-sandbox']});
 try{
  const page=await browser.newPage();
  const turn=styles('./TurnEvent.vue','data-v-turn-test'),tool=styles('./ConsoleToolEvent.vue','data-v-tool-test');
  for(const css of [tool+'\n'+turn,turn+'\n'+tool]){
   await page.setContent(`<style>${css}</style>
    <div id="thinking" data-v-turn-test class="tool-event live-status-event process-live thinking-only-event"><div class="live-process-summary"><span class="thinking-dots"><span></span></span></div></div>
    <div id="compression-live" data-v-turn-test class="tool-event live-status-event process-live">正在压缩上下文</div>
    <div id="retry" data-v-turn-test class="tool-event retry-inline-event process-live"><span class="retry-inline-state">等待重试</span><button class="retry-inline-cancel">取消重试</button></div>
    <div id="live-tool" data-v-turn-test class="tool-event live-inline-tool process-live">Read</div>
    <div id="notice" data-v-turn-test class="tool-event live-status-event agent-notice-event">通知</div>
    <details id="reasoning" data-v-turn-test class="reasoning-card tool-event"><summary>思考过程</summary></details>
    <details id="compaction" data-v-tool-test class="tool-event tool-succeeded" open><summary>上下文压缩</summary><div data-v-tool-test class="tool-detail"><div data-v-tool-test class="tool-result-tabbar"><button data-v-tool-test>结果</button></div></div></details>`);
   for(const dark of [false,true]){
    const result=await page.evaluate(dark=>{
     document.documentElement.classList.toggle('dark',dark);
     return {rows:[...document.querySelectorAll('.tool-event')].map(el=>{const c=getComputedStyle(el);return {id:el.id,borders:[c.borderTopWidth,c.borderRightWidth,c.borderBottomWidth,c.borderLeftWidth],shadow:c.boxShadow,background:c.backgroundColor};}),detail:getComputedStyle(document.querySelector('.tool-detail')).borderLeftWidth,tab:getComputedStyle(document.querySelector('.tool-result-tabbar button')).borderTopWidth};
    },dark);
    assert.equal(result.rows.length,7);
    for(const row of result.rows){assert.deepEqual(row.borders,['0px','0px','0px','0px'],`${dark?'dark':'light'} ${row.id}`);assert.equal(row.shadow,'none');}
    assert.equal(result.detail,'1px');assert.equal(result.tab,'1px');
    if(dark)assert.ok(result.rows.every(row=>row.background==='rgb(29, 30, 34)'),'existing dark background is preserved');
   }
  }
 }finally{await browser.close();}
});
