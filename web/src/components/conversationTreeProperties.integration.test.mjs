import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import * as defaults from './folderRunDefaults.js';

// Run the actual SFC's property load/change/validate/save functions in Node.
// No DOM, browser, animation sleeps or duplicate implementation of those rules.
const source = fs.readFileSync(new URL('./ConversationTree.vue', import.meta.url), 'utf8');
const display = fs.readFileSync(new URL('../views/consoleView/display.js', import.meta.url), 'utf8');
function between(text, start, end) {
  const from = text.indexOf(start), to = text.indexOf(end, from + start.length);
  assert.ok(from >= 0 && to > from, `${start}..${end}`);
  return text.slice(from, to);
}
const actual = between(source, 'async function showProperties(row) {', 'async function loadAllFolders()')
  + '\n' + between(display, 'export function modelThinkingLevels(', 'export function thinkingLabel(').replaceAll('export function', 'function');
const clone = value => JSON.parse(JSON.stringify(value));
const ref = value => ({value});
const flush = async () => {for (let i = 0; i < 12; i++) await Promise.resolve();};
function deferred() {let resolve; const promise = new Promise(yes => {resolve = yes;}); return {promise, resolve};}
const fallback = {mainModel:'fast', mainThinkingLevel:'high', mainFastMode:false, agentModel:'', agentThinkLevel:'', agentFastMode:null};
const models = [
  {key:'fast', thinkingLevels:['off','high'], defaultThinkingLevel:'high', supportsFast:true},
  {key:'slow', thinkingLevels:['low'], defaultThinkingLevel:'low', supportsFast:false},
  {key:'plain', thinkingLevels:[], defaultThinkingLevel:'', supportsFast:false},
];
function harness({local = {}, inherited = {}, getProperties, failOptions = false, impact} = {}) {
  const calls = [], events = [], warnings = [], errors = [];
  const stored = {local:clone(local)};
  const response = id => ({name:id,path:id,workspace:{local:'/project'},prompt:{local:'original'},
    runDefaults:{local:clone(stored.local),inherited:clone(inherited),fallback:clone(fallback),resolved:{},sources:{}}});
  const state = {
    ...defaults, ref, computed: getter => ({get value() {return getter();}}), watch: () => {}, nextTick: async () => {},
    thinkingLabel: value => value, apiError: error => error.message,
    propertiesDialog:ref(false), propertiesLoading:ref(false), propertiesSaving:ref(false), propertiesTab:ref('context'),
    propertyModelOptions:ref([]), propertiesRunDefaultsBaseline:ref({}), propertiesForm:{},
    impactState:{}, impactDialog:ref(false),
    ElMessage:{warning: text => warnings.push(text), error: text => errors.push(text), success: () => {}},
    CustomEvent: class {constructor(type, options) {this.type=type; this.detail=options.detail;}},
    window:{dispatchEvent: event => events.push(clone(event.detail))}, mergeLocatedFolders:()=>{}, emitRows:()=>{},
    Api:{
      conversationFolderProperties: id => getProperties ? getProperties(id, response) : Promise.resolve(response(id)),
      async rathOptions() {if(failOptions)throw Error('options unavailable');return {models};},
      async conversationFolderPropertiesImpact(id,payload) {calls.push({kind:'impact',id,payload:clone(payload)});return impact ? impact() : {affectedCount:0};},
      async updateConversationFolderProperties(id,payload) {calls.push({kind:'update',id,payload:clone(payload)});if(Object.hasOwn(payload,'runDefaults'))stored.local=clone(payload.runDefaults);return {updatedCount:2};},
      async locateConversationFolderInTree(id) {calls.push({kind:'locate',id});return {folderItems:[]};},
    },
  };
  const context=vm.createContext(state);
  vm.runInContext('let propertiesRequestGeneration=0; let propertiesSaveGeneration=0;\n'+actual,context);
  const run=code=>vm.runInContext(code,context);
  const open=async(id='project')=>run(`showProperties(${JSON.stringify(id==='__temporary'?{systemNode:'temporary'}:{folderId:id})})`);
  const select=(field,value)=>run(`setRunDefault(${JSON.stringify(field)},runDefaultOption(${JSON.stringify(value)}))`);
  return {state,calls,events,warnings,errors,stored,run,open,select,lastUpdate:()=>calls.filter(call=>call.kind==='update').at(-1)};
}

for (const id of ['project','__temporary']) {
  test(`${id}: context-only saves preserve removed models, false and null`, async () => {
    const local={mainModel:'removed',agentModel:'removed-agent',mainFastMode:false,agentFastMode:null};
    const h=harness({local});await h.open(id);
    h.state.propertiesForm.workspaceDir='/changed';h.state.propertiesForm.promptMarkdown='changed context';
    await h.run('saveProperties()');
    assert.deepEqual(h.stored.local,local);
    for (const call of h.calls.filter(call=>['impact','update'].includes(call.kind))) {
      assert.equal(Object.hasOwn(call.payload,'runDefaults'),false);
      assert.equal(Object.hasOwn(call.payload,'workspaceDir'),id!=='__temporary');
      assert.equal(call.payload.promptMarkdown,'changed context');
    }
    assert.equal(h.lastUpdate().id,id);
    assert.deepEqual(h.events,[{folderId:id}]);
    assert.equal(h.calls.some(call=>call.kind==='locate'),id!=='__temporary');
    assert.deepEqual(h.warnings,[]);assert.deepEqual(h.errors,[]);
  });
}

test('explicit defaults changes still validate, but restoring the original value is a no-op',async()=>{
  const h=harness({local:{mainModel:'removed',mainFastMode:false}});await h.open();
  h.select('mainFastMode',true);await h.run('saveProperties()');
  assert.match(h.warnings.at(-1),/已不可用/);assert.equal(h.calls.length,0);
  h.select('mainFastMode',false);await h.run('saveProperties()');
  assert.equal(Object.hasOwn(h.lastUpdate().payload,'runDefaults'),false);
});

test('model selection repairs dependent capabilities and preserves sparse follow values',async()=>{
  const h=harness();await h.open();
  h.select('mainModel','fast');h.select('mainThinkingLevel','high');h.select('mainFastMode',true);
  h.select('mainModel','slow');
  assert.equal(h.state.propertiesForm.runDefaults.mainThinkingLevel,'low');
  assert.equal(h.state.propertiesForm.runDefaults.mainFastMode,false);
  assert.deepEqual(clone(h.run('mainThinkingOptions.value')),['low']);
  h.select('agentModel','');h.select('agentThinkLevel','');h.select('agentFastMode',null);
  await h.run('saveProperties()');
  assert.deepEqual(h.lastUpdate().payload.runDefaults,{mainModel:'slow',mainThinkingLevel:'low',mainFastMode:false,agentModel:'',agentThinkLevel:'',agentFastMode:null});
  await h.open();h.run("setRunDefault('mainFastMode',RUN_DEFAULT_INHERIT)");await h.run('saveProperties()');
  assert.equal(Object.hasOwn(h.lastUpdate().payload.runDefaults,'mainFastMode'),false);
  await h.open();h.run('clearRunDefaults()');await h.run('saveProperties()');
  assert.deepEqual(h.lastUpdate().payload.runDefaults,{});
  await h.open();h.select('mainModel','plain');
  assert.deepEqual(clone(h.run('mainThinkingOptions.value')),['off']);
});

test('a changed parent capability does not block an unrelated context save',async()=>{
  const local={mainThinkingLevel:'high',mainFastMode:true,agentThinkLevel:'high',agentFastMode:true};
  const h=harness({local,inherited:{mainModel:'slow',agentModel:'slow'}});await h.open();
  h.state.propertiesForm.promptMarkdown='new context';await h.run('saveProperties()');
  assert.equal(Object.hasOwn(h.lastUpdate().payload,'runDefaults'),false);
  assert.deepEqual(h.stored.local,local);
});

test('options failure leaves context editable without rewriting defaults',async()=>{
  const h=harness({local:{mainModel:'removed'},failOptions:true});await h.open();
  h.state.propertiesForm.workspaceDir='/still-editable';await h.run('saveProperties()');
  assert.equal(Object.hasOwn(h.lastUpdate().payload,'runDefaults'),false);
  assert.match(h.warnings[0],/模型选项加载失败/);assert.deepEqual(h.errors,[]);
});

test('a late response from a closed folder cannot replace the active dialog or baseline',async()=>{
  const old=deferred();
  const h=harness({getProperties:(id,response)=>id==='old'?old.promise:Promise.resolve(response(id))});
  const loading=h.open('old');h.run('closeProperties()');await h.open('new');
  old.resolve({name:'old',runDefaults:{local:{mainModel:'removed'}}});await loading;
  assert.equal(h.state.propertiesForm.folderId,'new');
  assert.deepEqual(clone(h.state.propertiesRunDefaultsBaseline.value),{});
});

for (const choice of [true,false,null]) {
  test(`snapshot choice ${choice} retains preview, opt-in and cancel semantics`,async()=>{
    const h=harness({impact:()=>({affectedCount:2,updatableCount:2})});await h.open();
    h.state.propertiesForm.promptMarkdown='changed';const pending=h.run('saveProperties()');await flush();
    assert.equal(h.state.impactDialog.value,true);assert.equal(h.lastUpdate(),undefined);
    h.run(`finishImpact(${JSON.stringify(choice)})`);await pending;
    if(choice===null)assert.equal(h.lastUpdate(),undefined);
    else assert.equal(h.lastUpdate().payload.updateSnapshots,choice);
  });
}

test('closing a dialog while impact is pending prevents a stale save',async()=>{
  const pending=deferred();const h=harness({impact:()=>pending.promise});await h.open();
  const saving=h.run('saveProperties()');await flush();h.run('closeProperties()');
  pending.resolve({affectedCount:0});await saving;
  assert.equal(h.lastUpdate(),undefined);assert.deepEqual(h.events,[]);
});
