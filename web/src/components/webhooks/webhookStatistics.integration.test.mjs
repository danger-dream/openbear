import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {parse} from '@vue/compiler-sfc';
import {computed,ref,reactive,nextTick,watch,effectScope} from 'vue';
import {statusLabel,tabKey} from './webhookConfig.js';
const clone=value=>JSON.parse(JSON.stringify(value));
const fields=[{name:'sender',valueType:'string',queryable:true},{name:'amount',valueType:'number',queryable:true},{name:'done',valueType:'boolean',queryable:true},{name:'private',queryable:false}];
const deferred=()=>{let resolve;const promise=new Promise(yes=>{resolve=yes;});return {promise,resolve};};
function harness({props={},api={},live=false,file='./WebhookStatistics.vue'}={}) {
  const source=parse(fs.readFileSync(new URL(file,import.meta.url),'utf8')).descriptor.scriptSetup.content.replace(/^import .*;\n/gm,'');
  const calls=[],cleanup=[];const inputs=reactive({endpointId:'H1',global:false,...props});
  const Api={webhook:async id=>{calls.push(['schema',id]);return {endpoint:{config:{statistics:{businessFields:fields}}}};},webhookStatistics:async params=>{calls.push(['statistics',clone(params)]);return {items:[],nextCursor:null};},webhookEvents:async params=>{calls.push(['events',clone(params)]);return {items:[],nextCursor:null};},...api};
  const ctx={computed,ref,nextTick,watch:live?watch:()=>{},onBeforeUnmount:fn=>cleanup.push(fn),defineProps:()=>inputs,defineEmits:()=>()=>{},statusLabel,tabKey,Api,AbortController,console};
  const effects=effectScope();vm.createContext(ctx);effects.run(()=>vm.runInContext(source,ctx));
  return {run:code=>vm.runInContext(code,ctx),calls,props:inputs,api:Api,close(){for(const fn of cleanup)fn();effects.stop();}};
}
test('E11/F statistics ranking loads saved fields without editor props and waits for a selected field',async()=>{
  const h=harness({props:{statistics:{businessFields:[{name:'UNSAVED',queryable:true}]}}});h.run("tab.value='business';businessView.value='ranking'");
  await h.run('load()');assert.deepEqual(h.calls,[['schema','H1']]);assert.deepEqual(clone(h.run('queryFields.value')).map(x=>x.name),['sender','amount','done']);
  h.run("rankingField.value='sender'");await h.run('resetQuery()');assert.equal(h.calls.at(-1)[1].view,'ranking');assert.equal(h.calls.at(-1)[1].field,'sender');assert.equal(h.calls.filter(x=>x[0]==='schema').length,1);h.close();
});
test('E11 typed business filters preserve text, numeric zero and false; invalid numbers do not submit',async()=>{
  const h=harness();h.run("tab.value='business'");await h.run('load()');
  h.run("filterField.value='sender';filterValue.value='001';applyFilter();filterField.value='amount';filterValue.value='0';applyFilter();filterField.value='done';filterValue.value=false;applyFilter()");
  await h.run('resetQuery()');assert.deepEqual(JSON.parse(h.calls.at(-1)[1].filters),{sender:'001',amount:0,done:false});
  h.run("filterField.value='amount';filterValue.value='';applyFilter()");assert.match(h.run('filterError.value'),/有效数字/);assert.equal(h.run('appliedFilters.value.amount'),0);
  h.run("removeFilter('sender')");assert.deepEqual(clone(h.run('appliedFilters.value')),{amount:0,done:false});h.close();
});
test('E13 scope filters reach statistics and event APIs; page cursors retain the original time range',async()=>{
  const h=harness({props:{scopeType:'folder',scopeId:'F1'}});h.run("tab.value='phases'");await h.run('load()');
  const first=h.calls.at(-1)[1];assert.equal(first.scopeType,'folder');assert.equal(first.scopeId,'F1');
  h.run("tab.value='events';nextCursor.value='PAGE2'");await h.run('nextPage()');const second=h.calls.at(-1)[1];assert.equal(h.calls.at(-1)[0],'events');assert.equal(second.cursor,'PAGE2');assert.equal(second.start,first.start);assert.equal(second.end,first.end);assert.deepEqual(clone(h.run('previousCursors.value')),[null]);
  h.api.webhookEvents=async()=>{throw new Error('offline');};h.run("nextCursor.value='PAGE3'");await h.run('nextPage()');assert.deepEqual(clone(h.run('previousCursors.value')),[null]);assert.equal(h.run('currentCursor.value'),'PAGE2');h.close();
});
test('F12 late saved-schema response cannot overwrite a newer selected endpoint',async()=>{
  const pending=deferred();const h=harness({api:{webhook:id=>id==='H1'?pending.promise:Promise.resolve({endpoint:{config:{statistics:{businessFields:[{name:'newField',queryable:true}]}}}})}});
  h.run("tab.value='business'");const old=h.run('load()');h.props.endpointId='H2';await h.run('load()');pending.resolve({endpoint:{config:{statistics:{businessFields:fields}}}});await old;
  assert.deepEqual(clone(h.run('queryFields.value')).map(x=>x.name),['newField']);assert.equal(h.calls.length,1);assert.equal(h.calls[0][1].endpointId,'H2');h.close();
});
test('F12 leaving a saved endpoint clears stale filters and loading state through real watchers',async()=>{
  const h=harness({live:true});await nextTick();await nextTick();h.run("appliedFilters.value={sender:'old'};rankingField.value='sender';loading.value=true");h.props.endpointId=undefined;
  await nextTick();await nextTick();assert.equal(h.run('loading.value'),false);assert.deepEqual(clone(h.run('appliedFilters.value')),{});assert.equal(h.run('rankingField.value'),'');h.close();
});
test('E08 histogram uses backend cumulative buckets and preserves the final infinity bound',()=>{
  const h=harness();assert.deepEqual(clone(h.run('bucketRows({bounds:[0.1,0.5,1,null],counts:[2,1,1,0],cumulative:[2,3,4,4]})')),[{le:0.1,count:2},{le:0.5,count:3},{le:1,count:4},{le:'+∞',count:4}]);h.close();
});
test('E13 global query field catalog can populate ranking without borrowing a draft schema',async()=>{
  const h=harness({props:{endpointId:undefined,global:true,scopeType:'folder',scopeId:'F1'},api:{webhookStatistics:async()=>({items:[],queryableFields:[{name:'sender',valueType:'string'}]})}});h.run("tab.value='business';businessView.value='ranking'");await h.run('load()');assert.equal(h.run('queryFields.value[0].name'),'sender');assert.deepEqual(clone(h.run('data.value.items')),[]);h.close();
});
test('F07 statistics configuration edits are isolated; reopening discards unapplied edits and apply emits a copy',async()=>{
  const source=parse(fs.readFileSync(new URL('./WebhookStatisticsConfig.vue',import.meta.url),'utf8')).descriptor.scriptSetup.content.replace(/^import .*;\n/gm,'');
  const initial={dimensions:[{name:'source',path:'query.appname',kind:'category'}],metricDefinitions:[],businessFields:[{name:'sender',path:'body.sender',valueType:'string'}]};
  const props=reactive({modelValue:false,statistics:clone(initial)}),events=[];
  const ctx={ref,watch,clone,defineProps:()=>props,defineEmits:()=> (...args)=>events.push(clone(args))};
  const scope=effectScope();vm.createContext(ctx);scope.run(()=>vm.runInContext(source,ctx));const run=code=>vm.runInContext(code,ctx);
  try {
    props.modelValue=true;await nextTick();run("draft.value.dimensions[0].name='changed';draft.value.businessFields.splice(0,1)");
    assert.deepEqual(clone(props.statistics),initial);assert.deepEqual(events,[]);
    props.modelValue=false;await nextTick();props.modelValue=true;await nextTick();assert.deepEqual(clone(run('draft.value')),initial);
    run("draft.value.dimensions[0].name='applied';apply()");assert.equal(events[0][0],'apply');assert.equal(events[0][1].dimensions[0].name,'applied');assert.deepEqual(events[1],['update:modelValue',false]);
    assert.deepEqual(clone(props.statistics),initial);run("draft.value.dimensions[0].name='later'");assert.equal(events[0][1].dimensions[0].name,'applied');
  } finally { scope.stop(); }
});
