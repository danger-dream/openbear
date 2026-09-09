import { shallowReactive } from 'vue';
import Fuse from 'fuse.js';
import { pinyin } from 'pinyin-pro';
import { catalogKey } from './codec.js';

export const referenceCatalog = shallowReactive({items:[],ready:false,connected:false,stale:false,includeArchived:false,epoch:'',seq:0,version:null,treeStatus:null});
const records = new Map();
let searchIndex = new Fuse([], {includeScore:true,threshold:0.32,ignoreLocation:true,keys:[{name:'normalized',weight:0.6},{name:'pinyin',weight:0.2},{name:'initials',weight:0.12},{name:'pathText',weight:0.08}]});
let socket = null, timer = null, heartbeat = null, stopped = true, retry = 0, lastMessageAt = 0;
let overviewSubscription = null, overviewSerial = 0;
function sendOverviewSubscription() {
  if (socket?.readyState === 1 && overviewSubscription) socket.send(JSON.stringify({type:'conversation-overview',conversationUuid:overviewSubscription.uuid,subscriptionId:overviewSubscription.id}));
}
export function watchConversationOverview(uuid, listener) {
  const subscription = {uuid:String(uuid),id:String(++overviewSerial),listener};
  overviewSubscription = subscription;
  sendOverviewSubscription();
  return () => {
    if (overviewSubscription !== subscription) return;
    overviewSubscription = null;
    if (socket?.readyState === 1) socket.send(JSON.stringify({type:'conversation-overview',conversationUuid:'',subscriptionId:subscription.id}));
  };
}
export function applyConversationOverviewPacket(packet) {
  const current = overviewSubscription;
  if (!current || packet.subscriptionId !== current.id || packet.conversationUuid !== current.uuid) return false;
  current.listener(packet);
  return true;
}
export function normalizeQuery(value) { return String(value || '').normalize('NFKC').toLowerCase().trim(); }
export function searchableItem(item) {
  const label = String(item.label || item.name || '');
  const syllables = pinyin(label,{toneType:'none',type:'array',nonZh:'consecutive'});
  const initials = (label.match(/[\p{Script=Han}]+|[^\p{Script=Han}]+/gu)||[]).map(part=>/\p{Script=Han}/u.test(part)?pinyin(part,{toneType:'none',type:'array'}).map(word=>word[0]).join(''):part).join('');
  return {...item,normalized:normalizeQuery(label+' '+(item.name || '')),pinyin:normalizeQuery(syllables.join('')).replace(/\s/g,''),initials:normalizeQuery(initials).replace(/\s/g,''),pathText:normalizeQuery(item.group)};
}
function rebuild() {
  referenceCatalog.items = [...records.values()];
  searchIndex.setCollection([...records.values()]);
}
export function applyCatalogPacket(packet) {
  if (packet.type === 'snapshot') {
    records.clear();
    for (const row of packet.items || []) records.set(row.key,searchableItem(row));
    Object.assign(referenceCatalog,{epoch:packet.epoch,seq:packet.seq,includeArchived:Boolean(packet.includeArchived),ready:true,stale:false});
    rebuild();
  } else if (packet.type === 'patch') {
    if (packet.epoch !== referenceCatalog.epoch || packet.previousSeq !== referenceCatalog.seq) {
      if (packet.epoch === referenceCatalog.epoch && packet.seq <= referenceCatalog.seq) return true;
      referenceCatalog.stale = true; return false;
    }
    const affected = new Set([...(packet.removed || []), ...(packet.upserts || []).map(row=>row.key)]);
    if (affected.size) searchIndex.remove(item=>affected.has(item.key));
    for (const key of packet.removed || []) records.delete(key);
    for (const row of packet.upserts || []) { const item=searchableItem(row); records.set(row.key,item); searchIndex.add(item); }
    referenceCatalog.seq = packet.seq; referenceCatalog.stale = false;
    if (affected.size) referenceCatalog.items = [...records.values()];
  } else return true;
  if (packet.version) referenceCatalog.version = packet.version;
  if (packet.treeStatus) referenceCatalog.treeStatus = packet.treeStatus;
  return true;
}
export function referenceItem(ref) { void referenceCatalog.seq; return records.get(catalogKey(ref)); }
export function searchReferences(query='', {kind='',currentConversation='',limit=30,items=null}={}) {
  const q = normalizeQuery(query), compact = q.replace(/[\s._-]/g,'');
  const queryForms = /\p{Script=Han}/u.test(q) ? searchableItem({label:q}) : null;
  const candidates = items ? items.map(searchableItem) : referenceCatalog.items;
  const matches = new Map();
  for (const item of candidates) {
    if (kind && item.kind !== kind) continue;
    if (item.kind === 'chat' && item.id === currentConversation) continue;
    let score = Infinity;
    const label = normalizeQuery(item.label), full = item.pinyin, initials = item.initials;
    if (!q) score = 10;
    else if (label === q || normalizeQuery(item.name) === q) score = 0;
    else if (label.startsWith(q)) score = 1;
    else if (item.normalized.includes(q)) score = 2;
    else if (full.includes(compact) || (queryForms && full.includes(queryForms.pinyin))) score = 3;
    else if (initials.includes(compact) || (queryForms && initials.includes(queryForms.initials))) score = 4;
    else if (item.pathText.includes(q)) score = 5;
    if (Number.isFinite(score)) matches.set(item.key,{item,score});
  }
  if (q.length > 1) {
    const engine = items ? new Fuse(candidates,{includeScore:true,threshold:0.3,ignoreLocation:true,keys:['normalized','pinyin','initials']}) : searchIndex;
    for (const {item,score} of engine.search(compact,{limit:limit*3})) {
      if ((kind && item.kind!==kind) || (item.kind==='chat' && item.id===currentConversation)) continue;
      if (!matches.has(item.key)) matches.set(item.key,{item,score:6+(score || 0)});
    }
  }
  return [...matches.values()].sort((a,b)=>a.score-b.score || Number(b.item.updatedAt || 0)-Number(a.item.updatedAt || 0) || a.item.label.localeCompare(b.item.label,'zh-CN')).slice(0,limit).map(x=>x.item);
}
function sendScope() { if (socket?.readyState===1) socket.send(JSON.stringify({type:'resync',includeArchived:referenceCatalog.includeArchived})); }
export function includeArchivedReferences(value) {
  if (referenceCatalog.includeArchived===Boolean(value)) return;
  referenceCatalog.includeArchived=Boolean(value); referenceCatalog.stale=true; sendScope();
}
function connect() {
  if (stopped || typeof window==='undefined') return;
  const current = new WebSocket(`${location.protocol==='https:'?'wss:':'ws:'}//${location.host}/api/events/ws${referenceCatalog.includeArchived?'?archived=1':''}`);
  socket=current;
  current.onopen=()=>{if(socket!==current)return;referenceCatalog.connected=true;lastMessageAt=Date.now();retry=0;sendOverviewSubscription();};
  current.onmessage=event=>{
    if(socket!==current)return;
    lastMessageAt=Date.now();
    let packet;try{packet=JSON.parse(event.data);}catch{return;}
    if(packet.type==='conversation-overview'){applyConversationOverviewPacket(packet);return;}
    if(packet.type==='ping'){current.send(JSON.stringify({type:'ping'}));return;}
    if(packet.type==='resync'||packet.type==='stale'){referenceCatalog.stale=true;sendScope();return;}
    if(!applyCatalogPacket(packet))sendScope();
  };
  current.onerror=()=>{};
  current.onclose=event=>{
    if(socket!==current)return;
    socket=null;referenceCatalog.connected=false;referenceCatalog.stale=true;
    if(event.code===1008){stopReferenceCatalog({clear:true});return;}
    if(!stopped)timer=setTimeout(connect,Math.min(15000,800*2**Math.min(retry++,5)));
  };
}
function resume() {
  if (stopped || document.visibilityState==='hidden') return;
  if (socket?.readyState===1) sendScope();
  else if(!socket){clearTimeout(timer);connect();}
}
export function startReferenceCatalog() {
  if(!stopped || typeof window==='undefined')return;
  stopped=false;connect();
  window.addEventListener('pageshow',resume);document.addEventListener('visibilitychange',resume);
  heartbeat=setInterval(()=>{if(socket?.readyState===1 && Date.now()-lastMessageAt>65000)socket.close();},15000);
}
export function stopReferenceCatalog({clear=false}={}) {
  stopped=true;clearTimeout(timer);clearInterval(heartbeat);
  const old=socket;socket=null;old?.close();referenceCatalog.connected=false;
  if(typeof window!=='undefined'){window.removeEventListener('pageshow',resume);document.removeEventListener('visibilitychange',resume);}
  if(clear){overviewSubscription=null;records.clear();rebuild();Object.assign(referenceCatalog,{ready:false,stale:false,epoch:'',seq:0,version:null,treeStatus:null,includeArchived:false});}
}
