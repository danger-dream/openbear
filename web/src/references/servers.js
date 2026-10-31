import {shallowReactive,watch} from 'vue';
import {onSessionEnd} from '../sessionBoundary.js';
import {featureToggles} from '../featureToggles.js';
import {setServerFlagProbe,setServerPresenceProbe} from './presentation.js';
import {ensureFlagFont,flag} from '../servers/flags.js';

// The owner's servers as @ candidates. Live state (online, exec) changes far
// more often than the resource catalog, so this is a short-lived HTTP read on
// demand, never a second realtime channel. Only identity/display facts arrive.
export const serverDirectory = shallowReactive({items:[],byId:new Map(),ready:false,enabled:null,loading:false,error:'',loadedAt:0});
const FRESH_MS = 15000, STALE_MS = 60000;
let pending = null, generation = 0, attemptedAt = 0;
let loader = async () => (await import('../api.js')).Api.referenceServers();

export function setServerDirectoryLoader(fn) { loader = fn; }
export function serverReferencesOffered() {
 return !(featureToggles.known && featureToggles.servers === false) && serverDirectory.enabled !== false;
}
export function acceptServerDirectory(data) {
 const items = (data?.enabled === false ? [] : data?.items || []).map((row, order) => ({
  ...row, key:`server:${row.id}`, kind:'server', id:String(row.id), label:String(row.label || row.name || row.id),
  // Group is a detail of the row; one heading keeps the server list in its saved order.
  serverGroup:String(row.group || ''), group:'服务器', order,
 }));
 Object.assign(serverDirectory, {items, byId:new Map(items.map(item => [item.id, item])), enabled:data?.enabled !== false, ready:true, error:'', loadedAt:Date.now()});
 if (typeof document !== 'undefined' && items.some(item => flag(item.region))) ensureFlagFont();
 return serverDirectory;
}
export function loadServerDirectory({force=false}={}) {
 if (featureToggles.known && featureToggles.servers === false) {
  Object.assign(serverDirectory, {items:[], byId:new Map(), enabled:false, ready:true, error:''});
  return Promise.resolve(serverDirectory);
 }
 if (pending) return pending;
 if (!force && serverDirectory.ready && Date.now() - serverDirectory.loadedAt < FRESH_MS) return Promise.resolve(serverDirectory);
 const current = ++generation;
 attemptedAt = Date.now(); serverDirectory.loading = true;
 pending = Promise.resolve().then(() => loader())
  .then(data => { if (current === generation) acceptServerDirectory(data); },
   () => { if (current === generation) serverDirectory.error = '服务器列表暂时无法读取'; })
  .finally(() => { if (current === generation) { serverDirectory.loading = false; pending = null; } })
  .then(() => serverDirectory);
 return pending;
}
export function serverItem(id) { return serverDirectory.byId.get(String(id)); }
// 'present' | 'missing' | 'unknown'. Rendering a server capsule asks for a
// (throttled) directory read in the browser so deleted servers can be marked.
export function serverPresence(id) {
 const {ready, enabled, byId, loading, loadedAt} = serverDirectory;
 if (typeof window !== 'undefined' && !loading && Date.now() - attemptedAt > STALE_MS && (!ready || Date.now() - loadedAt > STALE_MS)) {
  attemptedAt = Date.now();
  queueMicrotask(() => { void loadServerDirectory(); });
 }
 if (!ready || enabled === false) return 'unknown';
 return byId.has(String(id)) ? 'present' : 'missing';
}
export function clearServerDirectory() {
 generation++; pending = null; attemptedAt = 0;
 Object.assign(serverDirectory, {items:[], byId:new Map(), ready:false, enabled:null, loading:false, error:'', loadedAt:0});
}
watch(() => featureToggles.servers, (enabled, previous) => {
 if (enabled && previous === false) clearServerDirectory();
}, {flush:'sync'});
onSessionEnd(clearServerDirectory);
setServerPresenceProbe(serverPresence);
setServerFlagProbe(id => flag(serverItem(id)?.region));
