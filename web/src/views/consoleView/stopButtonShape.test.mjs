import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {parse} from '@vue/compiler-sfc';
import {compile, createSSRApp} from 'vue';
import {renderToString} from 'vue/server-renderer';
import postcss from 'postcss';
const source=fs.readFileSync(new URL('./ConsoleComposer.vue',import.meta.url),'utf8');
const descriptor=parse(source).descriptor;
const template=descriptor.template.content;
const css=postcss.parse(descriptor.styles.map(s=>s.content).join('\n'));
const button=(stop)=>template.match(stop? /<button type="button" class="send-button stop-button"[\s\S]*?<\/button>/ : /<button type="button" class="send-button"[\s\S]*?<\/button>/)[0];
function declarations(selector){let result={};css.walkRules(selector,r=>r.walkDecls(d=>result[d.prop]=d.value));return result;}
test('send and stop are circular with the reference three state colors',()=>{
 assert.equal(declarations('.send-button')['border-radius'],'50%');
 assert.equal(declarations('.send-button').background,'#fff');
 assert.equal(declarations('.send-button:disabled').background,'#414141');
 assert.equal(declarations('.stop-button').background,'#ff5058');
 assert.equal(declarations('.stop-button').color,'#202020');
 assert.match(button(false),/M12 20V4M5 11l7-7 7 7/);
 assert.match(button(true),/<rect x="5" y="5" width="14" height="14" rx="1.5"/);
 assert.doesNotMatch(source,/toggleStopButtonShape|startStopShapePress|<Promotion/);
});
test('send respects canSend and stop preserves its action',async()=>{
 for(const canSend of [true,false]){
  const events=[];
  const render=compile(button(false));
  let vnode;
  const app=createSSRApp({render(){vnode=render.call(this,{props:{canSend},emit:e=>events.push(e)},[]);return vnode;}});
  const html=await renderToString(app);
  assert.equal(vnode.props.disabled,!canSend);
  assert.equal(/disabled/.test(html),!canSend);
  if(canSend){vnode.props.onClick();assert.deepEqual(events,['send']);}
 }
 const events=[];const render=compile(button(true));let vnode;
 await renderToString(createSSRApp({render(){vnode=render.call(this,{emit:e=>events.push(e)},[]);return vnode;}}));
 vnode.props.onClick();assert.deepEqual(events,['stop']);
});
