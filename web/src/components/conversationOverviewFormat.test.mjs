import test from 'node:test';
import assert from 'node:assert/strict';
import {formatOverviewDuration,vOverviewElapsed} from './conversationOverviewFormat.js';

for(const [ms,text] of [[null,'—'],[NaN,'—'],[-1,'—'],[0,'0 秒'],[550,'不到 1 秒'],[59000,'59 秒'],[60000,'1 分'],[3600000,'1 小时'],[67110000,'18 小时 38 分 30 秒'],[90061000,'1 天 1 小时 1 分 1 秒'],[259200000,'3 天']]){
 test(`overview duration is readable: ${String(ms)}`,()=>assert.equal(formatOverviewDuration(ms),text));
}
test('local live elapsed formatter ticks in hours and releases its timer when inactive/unmounted',()=>{
 const original={setInterval:globalThis.setInterval,clearInterval:globalThis.clearInterval,now:Date.now};let tick,active=false;
 try{
  Date.now=()=>70_000_000;globalThis.setInterval=fn=>{tick=fn;active=true;return 1;};globalThis.clearInterval=()=>{active=false;};
  const el={};const binding={value:{startAt:2_890_000,active:true}};
  vOverviewElapsed.mounted(el,binding);assert.equal(el.textContent,'18 小时 38 分 30 秒');assert.equal(active,true);
  Date.now=()=>70_001_000;tick();assert.equal(el.textContent,'18 小时 38 分 31 秒');
  vOverviewElapsed.updated(el,{value:{startAt:2_890_000,active:false}});assert.equal(active,false);assert.equal(el.textContent,'—');
  vOverviewElapsed.updated(el,binding);assert.equal(active,true);vOverviewElapsed.beforeUnmount(el);assert.equal(active,false);
 }finally{globalThis.setInterval=original.setInterval;globalThis.clearInterval=original.clearInterval;Date.now=original.now;}
});
