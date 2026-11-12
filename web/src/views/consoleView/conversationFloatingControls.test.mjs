import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {ref, nextTick} from 'vue';
import {parse} from '@vue/compiler-sfc';
import postcss from 'postcss';

const read=file=>fs.readFileSync(new URL(file,import.meta.url),'utf8');
const source=read('./ConsoleView.vue');
function between(start,end){const a=source.indexOf(start),b=source.indexOf(end,a+start.length);assert.ok(a>=0&&b>a);return source.slice(a,b);}
function css(file,selector,width=390){const styles=parse(read(file)).descriptor.styles;const out={};
  postcss.parse(styles.map(s=>s.content).join('\n')).walkRules(rule=>{
    if(!rule.selectors.includes(selector))return;
    for(let p=rule.parent;p;p=p.parent)if(p.type==='atrule'&&p.name==='media'){
      if(/max-width:\s*(\d+)px/.test(p.params)&&width>Number(RegExp.$1))return;
      if(/min-width:\s*(\d+)px/.test(p.params)&&width<Number(RegExp.$1))return;
    }
    rule.walkDecls(d=>out[d.prop]=d.value);
  });return out;
}
function geometry(){
  const el={scrollHeight:2000,clientHeight:500,scrollTop:1500};
  const c=vm.createContext({scroller:ref(el),scrollerOverflow:ref(false),scrollerAwayFromBottom:ref(false),SCROLL_BOTTOM_THRESHOLD:80});
  vm.runInContext(between('function scrollerDistanceFromBottom(', 'function markUserScrollIntent('),c);
  return {c,el};
}

test('return-to-bottom visibility follows actual distance, including short pages and a missing scroller',()=>{
  const {c,el}=geometry();
  for(const [top,expected] of [[1500,false],[1420,false],[1419,true],[0,true]]){
    el.scrollTop=top;c.updateScrollerOverflow();assert.equal(c.scrollerAwayFromBottom.value,expected);
  }
  el.scrollHeight=300;c.updateScrollerOverflow();assert.equal(c.scrollerAwayFromBottom.value,false);assert.equal(c.scrollerOverflow.value,false);
  c.scroller.value=null;c.updateScrollerOverflow();assert.equal(c.scrollerAwayFromBottom.value,false);
});

test('content growth is observed without moving a history reader, and observers are released on replacement',()=>{
  const {c,el}=geometry();let callback,cleanup,disconnected=false,observed;
  Object.assign(c,{ref,watch:(_target,fn)=>{callback=fn;},ResizeObserver:class{constructor(fn){this.sync=fn;c.observer=this;}observe(node){observed=node;}disconnect(){disconnected=true;}}});
  vm.runInContext(between('const timelineContent = ref(null);','const activeTurnIndex ='),c);
  const content={};callback(content,null,fn=>{cleanup=fn;});assert.equal(observed,content);
  el.scrollHeight+=200;c.observer.sync();assert.equal(c.scrollerAwayFromBottom.value,true);assert.equal(el.scrollTop,1500);
  cleanup();assert.equal(disconnected,true);
});

test('the down button returns to real latest content, clears a search window and enables follow',async()=>{
  const {c,el}=geometry();el.scrollTop=0;
  Object.assign(c,{nextTick,autoScrollLocked:ref(false),pinnedActiveTurnIndex:3,searchNavigationGeneration:0,
    searchLocating:ref(true),searchReturnPosition:ref({savedBeforeDisplaySeq:10,savedHasMoreBefore:true}),
    searchWindowActive:ref(true),searchBaseOperationIds:new Set(['old']),nextBeforeDisplaySeq:ref(1),hasMoreBefore:ref(false),
    releaseSearchWindow:()=>{c.released=true;},runProgrammaticScroll:fn=>fn(),scheduleActiveTurnFromScroll:()=>{}});
  vm.runInContext(between('async function scrollBottom(', 'function scheduleScrollBottom('),c);
  vm.runInContext(between('function lockAutoScroll(', 'function unlockAutoScroll('),c);
  vm.runInContext(between('function returnToLatestFromSearch(', 'async function loadEarlierOperations('),c);
  c.returnToLatestFromSearch();await new Promise(setImmediate);
  assert.equal(el.scrollTop,el.scrollHeight);assert.equal(c.autoScrollLocked.value,true);assert.equal(c.scrollerAwayFromBottom.value,false);
  assert.equal(c.searchReturnPosition.value,null);assert.equal(c.searchWindowActive.value,false);assert.equal(c.released,true);
  assert.equal(c.nextBeforeDisplaySeq.value,10);assert.equal(c.hasMoreBefore.value,true);assert.equal(c.searchBaseOperationIds.size,0);
  assert.match(source,/class="scroll-bottom-button"[^>]+@click\.stop="returnToLatestFromSearch"/);
});

test('bottom collapse reveals its own offscreen summary under a programmatic guard without changing follow',()=>{
  let top=-200,guards=0;
  const summary={getBoundingClientRect:()=>({top,bottom:top+32})};
  const scroll={scrollTop:600,contains:el=>el===summary,getBoundingClientRect:()=>({top:60,bottom:560})};
  const c=vm.createContext({scroller:ref(scroll),autoScrollLocked:ref(false),runProgrammaticScroll:(fn,duration)=>{guards++;assert.equal(duration,400);fn();}});
  vm.runInContext(between('function scrollerViewportRect(', 'function scrollerTurnBoxes(') + between('function revealWorkSummary(', '// Height animation preserves'),c);
  c.revealWorkSummary(summary);assert.equal(scroll.scrollTop,328);assert.equal(guards,1);assert.equal(c.autoScrollLocked.value,false);
  top=100;c.revealWorkSummary(summary);assert.equal(guards,1);
  top=-200;c.autoScrollLocked.value=true;c.revealWorkSummary(summary);assert.equal(guards,1);
  c.revealWorkSummary({});assert.equal(guards,1);
});

test('the visible reading area excludes content under the glass, including collapse navigation',()=>{
  const summary={getBoundingClientRect:()=>({top:20,bottom:52})};
  const column={getBoundingClientRect:()=>({top:60})};
  const scroll={scrollTop:600,contains:el=>el===summary,closest:()=>column,
    getBoundingClientRect:()=>({top:0,bottom:560,height:560,left:0,right:390,width:390})};
  const c=vm.createContext({scroller:ref(scroll),autoScrollLocked:ref(false),runProgrammaticScroll:fn=>fn()});
  vm.runInContext(between('function scrollerViewportRect(', 'function scrollerTurnBoxes(') + between('function revealWorkSummary(', '// Height animation preserves'),c);
  assert.equal(c.scrollerViewportRect(scroll).top,60);assert.equal(c.scrollerViewportRect(scroll).height,500);
  c.revealWorkSummary(summary);assert.equal(scroll.scrollTop,548);
  for(const width of [320,390,760,1440]){
    const viewport=css('./ConsoleView.vue','.console-scroll',width),workspace=css('./ConsoleView.vue','.console-workspace',width);
    assert.equal(viewport['margin-top'],'calc(-1 * var(--console-header-height))');
    assert.equal(workspace['margin-top'],viewport['margin-top']);
    assert.equal(viewport['padding-top'],'var(--console-header-height)');
    assert.equal(workspace['padding-top'],viewport['padding-top']);
    assert.equal(viewport['scroll-padding-top'],'calc(var(--console-header-height) + 12px)');
  }
});

test('scroll lock stays below the full minimap when the composer grows',()=>{
  const page=css('./ConsoleView.vue','.console-page',1440);
  const bottom=page['--console-float-rail-bottom'];
  assert.match(bottom,/^min\(/);
  for(const variable of ['--console-composer-height','--console-float-minimap-top','--console-float-minimap-max-height','--console-float-control-gap','--console-float-control-size']) assert.ok(bottom.includes(variable));
  assert.equal(css('./ConsoleView.vue','.scroll-lock-toggle',1440).bottom,'var(--console-float-rail-bottom)');
  assert.match(css('./TurnMinimap.vue','.turn-minimap-rail',1440)['max-height'],/var\(--console-float-minimap-max-height/);
  assert.equal(css('./ConsoleView.vue','.console-controls',390).display,'none');
});

test('glass does not change header geometry; floating controls have distinct hit areas and avoid editor/selection overlays',()=>{
  const glass=css('./ConsoleHeader.vue','.console-header::before');
  assert.equal(glass['pointer-events'],'none');assert.match(glass['backdrop-filter'],/blur\(24px\)/);assert.match(glass['-webkit-backdrop-filter'],/blur\(24px\)/);assert.match(glass['mask-image'],/18px/);
  assert.equal(css('./ConsoleHeader.vue','.console-header').height,'calc(60px + env(safe-area-inset-top, 0px))');
  for(const width of [320,390,760,1440]){
    const dock=css('./ConsoleView.vue','.scroll-bottom-dock',width),button=css('./ConsoleView.vue','.scroll-bottom-button',width);
    assert.match(dock.bottom,/--console-composer-height/);assert.equal(dock.left,'50%');assert.equal(dock['pointer-events'],'none');
    assert.equal(button.width,'44px');assert.equal(button.height,'44px');assert.equal(button['pointer-events'],'auto');
  }
  assert.match(source,/v-if="scrollerAwayFromBottom && !contextEditorOpen"/);
  assert.equal(css('./ConsoleView.vue','.console-page.visibility-overlay-active .scroll-bottom-dock').display,'none');
  const collapse=css('./ConversationProcessEvent.vue','.process-collapse-dock');
  assert.equal(collapse.position,'sticky');assert.equal(collapse.bottom,'12px');assert.equal(collapse['justify-content'],'flex-end');
  assert.equal(css('./ConversationProcessEvent.vue','.process-collapse-dock',1440).display,'none');
});
