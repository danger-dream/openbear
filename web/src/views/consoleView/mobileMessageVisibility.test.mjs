import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {parse} from '@vue/compiler-sfc';
import postcss from 'postcss';
import {ref,computed,effectScope} from 'vue';
import {createMessageVisibility,selectVisibilityRow} from './messageVisibility.js';
const files=['MessageVisibilityAction.vue','MessageVisibilityMobileMenu.vue','MessageVisibilityBar.vue','TurnList.vue','TurnWorkDetailPanel.vue','ConsoleView.vue'];
const descriptors=Object.fromEntries(files.map(f=>[f,parse(fs.readFileSync(new URL(f,import.meta.url),'utf8')).descriptor]));
function css(file,selector,width=390){const out={};postcss.parse(descriptors[file].styles.map(s=>s.content).join('\n')).walkRules(rule=>{
  if(!rule.selectors.includes(selector))return;
  for(let p=rule.parent;p;p=p.parent)if(p.type==='atrule'&&p.name==='media'&&!p.params.split(',').some(q=>[...q.matchAll(/\(([^)]+)\)/g)].every(([,s])=>{const[k,v]=s.split(':').map(x=>x.trim());return k==='min-width'?width>=parseFloat(v):k==='max-width'?width<=parseFloat(v):k==='hover'?v==='none':k==='pointer'&&v==='coarse';})))return;
  rule.walkDecls(d=>out[d.prop]=d.value);
});return out;}

test('phone transcript uses long press and only selection reserves a rail; work details keep their original layout',()=>{
  for(const width of [320,390,430,760]){
    const action='MessageVisibilityAction.vue';
    assert.equal(css(action,'.message-visibility-action.uses-long-press:not(.is-selecting)',width).display,'none');
    assert.equal(css(action,'.visibility-desktop-more',width).display,'none');
    assert.equal(css(action,'.message-visibility-action.placement-gutter',width).position,'absolute');
    assert.equal(css(action,'.message-visibility-action.placement-footer.is-selecting',width).left,'-12px');
    assert.equal(css(action,'.visibility-select input',width).width,'44px');
    assert.equal(css(action,'.visibility-select input',width).opacity,'0');
    assert.equal(css(action,'.visibility-check',width)['border-radius'],'50%');
    for(const f of ['TurnList.vue']){
      assert.equal(css(f,'.visibility-selected',width).background,undefined);
      assert.equal(css(f,'.visibility-selected::after',width).width,'2px');
    }
    assert.equal(css('TurnList.vue','.timed-row.visibility-target',width)['padding-left'],undefined);
    assert.equal(css('TurnList.vue','.timed-row.visibility-selectable',width)['padding-left'],'32px');
    assert.equal(css('TurnWorkDetailPanel.vue','.work-detail-entry',width).display,'block');
    assert.equal(css('TurnWorkDetailPanel.vue','.work-visibility-action',width).position,undefined);
  }
  assert.equal(css('MessageVisibilityAction.vue','.visibility-mobile-more',1440).display,'none');
  assert.match(css('TurnList.vue','.visibility-menu-target')['box-shadow'],/rgb\(var\(--ob-shadow-rgb\)/);
  assert.equal(css('TurnList.vue','.visibility-menu-target',1440)['box-shadow'],undefined);
  assert.match(descriptors['MessageVisibilityMobileMenu.vue'].template.content,/当前操作的消息/);
  assert.match(descriptors['ConsoleView.vue'].scriptSetup.content,/preserveSelectionPosition: \(\) => window\.matchMedia\('\(max-width: 760px\)'\)\.matches/);
  assert.equal(css('TurnList.vue','.timed-row-assistant.visibility-selectable:has(> .retry-inline-event)')['align-items'],'center');
  assert.match(descriptors['TurnList.vue'].template.content,/v-message-long-press=/);
  assert.match(descriptors['TurnList.vue'].template.content,/mobile-long-press/);
  assert.doesNotMatch(descriptors['TurnWorkDetailPanel.vue'].template.content,/v-message-long-press|mobile-long-press|MessageVisibilityAction|visibility-select/);
});

test('phone batch bar floats above composer but stays out of work details',()=>{
  const f='MessageVisibilityBar.vue',bar=css(f,'.message-visibility-bar');
  assert.equal(bar.position,'absolute');assert.equal(bar.width,'max-content');assert.equal(bar['z-index'],'60');
  assert.match(bar.bottom,/console-composer-height/);
  assert.equal(css(f,'.message-visibility-bar.is-work-open').display,'none');
  assert.equal(css(f,'.visibility-bar-actions button')['min-height'],'44px');
  assert.equal(css('ConsoleView.vue','.console-page.visibility-overlay-active .conversation-timeline-shell')['padding-bottom'],'96px');
  assert.equal(css('ConsoleView.vue','.console-page.visibility-overlay-active :deep(.work-detail-body)')['padding-bottom'],undefined);
  assert.match(descriptors['ConsoleView.vue'].template.content,/<MessageVisibilityBar\/>/);
});

test('touch row selection toggles once, while checkbox, selected text and busy state keep their owners',()=>{
  let n=0,prevented=0;
  const v={selecting:ref(true),busy:ref(false),canTarget:()=>true,toggle:()=>n++};
  const event={target:{closest:()=>null},preventDefault:()=>prevented++,stopPropagation(){}};
  const phone={matchMedia:()=>({matches:false}),getSelection:()=>''};
  selectVisibilityRow(event,{id:'a'},v,phone);assert.equal(n,1);assert.equal(prevented,1);
  selectVisibilityRow({...event,target:{closest:()=>({})}},{id:'a'},v,phone);assert.equal(n,1);
  selectVisibilityRow(event,{id:'a'},v,{...phone,getSelection:()=> '文字'});assert.equal(n,1);
  v.busy.value=true;selectVisibilityRow(event,{id:'a'},v,phone);assert.equal(n,1);
  v.selecting.value=false;selectVisibilityRow(event,{id:'a'},v,phone);assert.equal(n,1);
});

test('one shared mobile sheet routes single, round and selection actions and clears target on conversation switch',async()=>{
  const scope=effectScope(),uuid=ref('A'),calls=[];
  const operations=ref(new Map(['u','a','t'].map((opId,i)=>[opId,{opId,opType:['user_message','assistant_message','tool'][i]}])));
  const v=scope.run(()=>createMessageVisibility({conversationUuid:uuid,operations,api:{updateMessageVisibility:async(id,data)=>{calls.push(data);return{visibility:{conversationUuid:id,revision:calls.length,hiddenIds:[],items:[]}};}}}));
  const context=vm.createContext({computed,useMessageVisibility:()=>v,onMounted(){},onBeforeUnmount(){}});
  vm.runInContext(descriptors['MessageVisibilityMobileMenu.vue'].scriptSetup.content.replace(/^import .*;\n/gm,''),context);
  const turn={events:[{id:'a'},{id:'u'},{id:'t'}]};
  try{
    v.openMobileMenu({id:'a'},turn);vm.runInContext("choose('turn')",context);await Promise.resolve();
    assert.deepEqual(calls[0].opIds,['a','t']);assert.equal(v.mobileMenu.value,null);
    v.openMobileMenu({id:'a'},turn);vm.runInContext("choose('single')",context);await Promise.resolve();
    assert.deepEqual(calls[1].opIds,['a']);
    v.openMobileMenu({id:'a'},turn);vm.runInContext("choose('select')",context);
    assert.equal(v.selecting.value,true);assert.deepEqual([...v.selected.value],['a']);assert.equal(v.mobileMenu.value,null);
    v.cancelSelection();v.openMobileMenu({id:'a'},turn);uuid.value='B';assert.equal(v.mobileMenu.value,null);
    vm.runInContext("choose('turn')",context);assert.equal(calls.length,2);
    assert.match(descriptors['MessageVisibilityMobileMenu.vue'].template.content,/direction="btt"/);
    assert.match(css('MessageVisibilityMobileMenu.vue','.visibility-mobile-sheet.el-drawer')['max-height'],/mobile-viewport-height/);
  }finally{scope.stop();}
});
