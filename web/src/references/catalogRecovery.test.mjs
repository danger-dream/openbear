import test from 'node:test';
import assert from 'node:assert/strict';
import * as catalog from './catalog.js';

function fixture(t) {
  catalog.stopReferenceCatalog({clear:true});
  const names=['window','document','location','WebSocket','setTimeout','clearTimeout','setInterval','clearInterval'];
  const originals=new Map(names.map(name=>[name,Object.getOwnPropertyDescriptor(globalThis,name)]));
  const timers=new Map(),intervals=new Map(),sockets=[];let serial=0;
  const target=()=>({listeners:new Map(),addEventListener(name,fn){const group=this.listeners.get(name)||new Set();group.add(fn);this.listeners.set(name,group);},removeEventListener(name,fn){this.listeners.get(name)?.delete(fn);},emit(name){for(const fn of this.listeners.get(name)||[])fn();}});
  class Socket {
    constructor(url){this.url=url;this.readyState=0;this.sent=[];sockets.push(this);}
    open(){this.readyState=1;this.onopen?.();}
    send(text){this.sent.push(JSON.parse(text));}
    receive(packet){this.onmessage?.({data:JSON.stringify(packet)});}
    closed(code=1006){this.readyState=3;this.onclose?.({code});}
    close(){this.closed();}
  }
  Object.assign(globalThis,{window:target(),document:{...target(),visibilityState:'visible'},location:{protocol:'http:',host:'test.invalid'},WebSocket:Socket,
    setTimeout:(fn,delay)=>{const id=++serial;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id),
    setInterval:(fn,delay)=>{const id=++serial;intervals.set(id,{fn,delay});return id;},clearInterval:id=>intervals.delete(id)});
  t.mock.method(Math,'random',()=>0.5);
  t.after(()=>{catalog.stopReferenceCatalog({clear:true});for(const [name,descriptor] of originals){if(descriptor)Object.defineProperty(globalThis,name,descriptor);else delete globalThis[name];}});
  const start=()=>{catalog.startReferenceCatalog();return sockets.at(-1);};
  const fire=()=>{assert.equal(timers.size,1);const [id,item]=timers.entries().next().value;timers.delete(id);item.fn();return item.delay;};
  return {start,fire,timers,intervals,sockets};
}
const snapshot=(seq=1,includeArchived=false,epoch='e')=>({type:'snapshot',epoch,seq,includeArchived,items:[]});

test('one overflow marker and 127 stale patches cause one resync and retain post-snapshot updates',t=>{
  const h=fixture(t),socket=h.start();socket.open();socket.receive(snapshot());
  socket.receive({type:'resync'});
  for(let seq=2;seq<=128;seq++)socket.receive({type:'patch',epoch:'e',previousSeq:seq+1,seq:seq+2});
  assert.equal(socket.sent.length,1);assert.equal(socket.sent[0].type,'resync');
  socket.receive(snapshot(200));
  socket.receive({type:'patch',epoch:'e',previousSeq:200,seq:201,upserts:[{key:'doc:1',kind:'doc',id:'1',label:'新文档'}]});
  assert.equal(catalog.referenceCatalog.seq,201);assert.equal(catalog.referenceCatalog.items.length,1);
  assert.equal(catalog.referenceCatalog.stale,false);assert.equal(socket.sent.length,1);
});

test('archived choice made during CONNECTING survives initial old-scope snapshot and is sent once',t=>{
  const h=fixture(t),socket=h.start();catalog.includeArchivedReferences(true);socket.open();
  window.emit('pageshow');document.emit('visibilitychange');
  assert.equal(socket.sent.length,0,'wait for the server initial snapshot, not parallel resyncs');
  socket.receive(snapshot(1,false));
  assert.equal(catalog.referenceCatalog.includeArchived,true);
  assert.deepEqual(socket.sent.map(p=>p.includeArchived),[true]);
  socket.receive(snapshot(2,true));assert.equal(catalog.referenceCatalog.stale,false);assert.equal(h.timers.size,0);
});

test('rapid scope changes coalesce to newest intent and an old response cannot release another scope gate',t=>{
  const h=fixture(t),socket=h.start();socket.open();socket.receive(snapshot());
  catalog.includeArchivedReferences(true);catalog.includeArchivedReferences(false);
  for(let i=0;i<20;i++)socket.receive({type:'stale'});
  assert.deepEqual(socket.sent.map(p=>p.includeArchived),[true]);
  socket.receive(snapshot(1,false));assert.equal(socket.sent.length,1,'old initial snapshot does not settle pending true');
  socket.receive(snapshot(2,true));
  assert.equal(catalog.referenceCatalog.includeArchived,false);
  assert.deepEqual(socket.sent.map(p=>p.includeArchived),[true,false]);
  socket.receive(snapshot(3,false));assert.equal(catalog.referenceCatalog.stale,false);
});

test('accepted handshake without valid first snapshot backs off rather than resetting every time',t=>{
  const h=fixture(t);h.start();const delays=[];
  for(let i=0;i<7;i++){const socket=h.sockets.at(-1);socket.open();socket.closed();delays.push(h.fire());}
  assert.deepEqual(delays,[800,1600,3200,6400,12800,15000,15000]);
  const healthy=h.sockets.at(-1);healthy.open();healthy.receive(snapshot());healthy.closed();
  assert.equal(h.fire(),800,'valid snapshot restores normal retry budget');
});

test('snapshot timeout reconnects with backoff, and resume events cannot bypass the scheduled delay',t=>{
  const h=fixture(t),socket=h.start();socket.open();
  assert.equal(h.fire(),10000);assert.equal(socket.readyState,3);
  window.emit('pageshow');document.emit('visibilitychange');
  assert.equal(h.sockets.length,1);assert.equal(h.fire(),800);assert.equal(h.sockets.length,2);
  assert.equal(socket.sent.length,0);
});

test('duplicate lifecycle calls and old callbacks stay isolated; auth close cancels every timer',t=>{
  const h=fixture(t),old=h.start();catalog.startReferenceCatalog();assert.equal(h.sockets.length,1);
  old.open();catalog.stopReferenceCatalog({clear:true});assert.equal(h.timers.size,0);assert.equal(h.intervals.size,0);
  const current=h.start();current.open();current.receive(snapshot(5,false,'new'));
  old.onopen();old.receive(snapshot(999,false,'old'));old.onclose({code:1008});
  assert.equal(catalog.referenceCatalog.epoch,'new');assert.equal(catalog.referenceCatalog.connected,true);
  current.closed(1008);assert.equal(h.timers.size,0);assert.equal(h.intervals.size,0);assert.equal(catalog.referenceCatalog.ready,false);
});
