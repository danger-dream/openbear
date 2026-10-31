import { reactive, shallowRef } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { errorText, safeError } from '../serverHelpers.js';
import { MeshApi } from './meshApi.js';
import { draftOf, meshCodeText, reinstallErrorText } from './meshHelpers.js';

// Mesh codes first; other codes go to the servers page wording, free text (a
// data plane message) is shown as it is.
export function meshErrorText(raw) {
  const value = String(raw || '');
  return meshCodeText(value) || (/^[a-z][a-z0-9_]*$/.test(value) ? errorText(value) : value);
}
export const meshError = e => meshCodeText(e?.response?.data?.error) || safeError(e);

// One copy of the mesh for the page and the server cards. `work` is what the
// user edits; every edit is written to the draft with PUT. A poll never
// replaces edits that are not saved yet (in flight, or rejected with errors).
export const mesh = reactive({data: null, work: null, errors: [], rejected: false, saving: false, installing: false, loadError: '', error: '', unavailable: false});
let saving = null, again = false, loading = null;
// The draft as the server last had it (JSON). Reactive: the draft being edited is not swapped for an
// equal copy, so whatever asks unsaved() must still see a save come back.
const synced = shallowRef('');
// Bumped when a change to the mesh (a draft save, an action, a layout save) starts and when it ends:
// a poll that was out meanwhile may have been answered before the change and is dropped whole.
let revision = 0;

const snapshot = () => JSON.stringify(mesh.work);
// The rule under the pointer, in the board's legend or the panel's list: the board highlights its members.
export const ruleHover = reactive({id: ''});
export const unsaved = () => Boolean(mesh.work) && snapshot() !== synced.value;

function adopt(data, force = false) {
  const state = data?.mesh || data;
  if (!state || !Array.isArray(state.members)) return;
  const next = draftOf(state), text = JSON.stringify(next);
  // The same draft as the one being edited is not swapped in: nothing that reads it is worked out again.
  if ((force || !unsaved()) && text !== snapshot()) mesh.work = next;
  synced.value = text;
  mesh.data = state;
  adoptLayout(state.layout);
  mesh.unavailable = false;
  mesh.loadError = '';
  if (!mesh.rejected) {
    const errors = state.errors || [];
    if (JSON.stringify(errors) !== JSON.stringify(mesh.errors)) mesh.errors = errors;
  }
}

export function refresh() {
  if (loading) return loading;
  const mine = revision;
  loading = MeshApi.state().then(data => { if (mine === revision && !saving && !layoutSending) adopt(data); }).catch(e => {
    if (e?.response?.status === 404) mesh.unavailable = true;
    else mesh.loadError = meshError(e);
  }).finally(() => { loading = null; });
  return loading;
}

export function save() {
  if (!mesh.work) return Promise.resolve();
  if (saving) { again = true; return saving; }
  const body = snapshot();
  mesh.saving = true; revision++;
  saving = (async () => {
    try {
      const data = await MeshApi.saveDraft(JSON.parse(body));
      mesh.rejected = false; mesh.errors = []; mesh.error = '';
      const state = Array.isArray((data?.mesh || data)?.members) ? data : await MeshApi.state();
      // The server may fill in ids or addresses; take them unless the user typed on meanwhile.
      adopt(state, snapshot() === body);
    } catch (e) {
      const errors = e?.response?.data?.errors;
      if (e?.response?.status === 400 && Array.isArray(errors) && errors.length) { mesh.errors = errors; mesh.rejected = true; }
      else mesh.error = meshError(e);
    } finally {
      saving = null; mesh.saving = false; revision++;
      if (again) { again = false; if (unsaved()) void save(); }
    }
  })();
  return saving;
}

// Back to the draft as the server has it, dropping edits it rejected.
export function revert() {
  mesh.rejected = false;
  mesh.work = JSON.parse(synced.value || 'null');
  mesh.errors = mesh.data?.errors || [];
}

// Board positions: {serverId: {x, y}}. Not part of the draft. Moves are kept
// here at once and sent in one PUT shortly after the drag ends; positions from
// the server fill in everything not waiting to be sent. Without the endpoint
// (404) the positions stay on this page.
export const layout = reactive({positions: {}, unavailable: false});
const unsent = {};
let layoutTimer = null, layoutSending = null;
function adoptLayout(server) {
  if (!server || typeof server !== 'object') return;
  for (const [id, p] of Object.entries(server)) {
    if (id in unsent || !p || !Number.isFinite(p.x) || !Number.isFinite(p.y)) continue;
    const here = layout.positions[id];
    if (!here || here.x !== p.x || here.y !== p.y) layout.positions[id] = {x: p.x, y: p.y};
  }
}
// Position a node; sent later unless `keep` is false (an automatic place nobody chose).
export function place(id, position, keep = true) {
  const p = position && {x: Math.round(position.x), y: Math.round(position.y)};
  if (p) layout.positions[id] = p; else delete layout.positions[id];
  if (keep) unsent[id] = p || null;
}
export function saveLayout(delay = 600) {
  clearTimeout(layoutTimer);
  layoutTimer = setTimeout(sendLayout, delay);
}
async function sendLayout() {
  if (layoutSending) { await layoutSending; return saveLayout(0); }
  const positions = {...unsent};
  if (!Object.keys(positions).length || layout.unavailable) return;
  const sent = () => { for (const [id, p] of Object.entries(positions)) if (JSON.stringify(unsent[id]) === JSON.stringify(p)) delete unsent[id]; };
  // The reply is the whole merged layout.
  revision++;
  layoutSending = MeshApi.saveLayout(positions).then(data => { sent(); adoptLayout(data?.layout); }).catch(e => {
    const status = e?.response?.status, code = e?.response?.data?.error;
    // A rejected batch (a server that is not ours, bad numbers) is dropped; the places stay on this page.
    if (status === 400 || code === 'server_not_found') sent();
    else if (status === 404) layout.unavailable = true;
    else saveLayout(5000);
  }).finally(() => { layoutSending = null; revision++; });
  await layoutSending;
}

// Punched direct links, as each end reports them. GET /mesh carries
// nodes[id].summary.direct = {peerId: {state, via, rttMs}}; older controllers do
// not, and the board reads status.peers of GET /members/:id instead (peerLive).
// The selected link reads both ends' details for since, attempts and lastError (linkLive).
export const peerLive = reactive({});
export const linkLive = reactive({id: '', peers: {}});
export function keepPeers(id, detail) {
  const peers = detail?.status?.peers;
  if (Array.isArray(peers)) peerLive[id] = peers; else delete peerLive[id];
}
export const loadPeers = ids => Promise.all(ids.map(id => MeshApi.member(id).then(d => keepPeers(id, d), () => {})));
// The summary's direct peers in the shape of status.peers; null when the controller does not send them.
export function summaryPeers(id) {
  const direct = mesh.data?.nodes?.[id]?.summary?.direct;
  if (!direct || typeof direct !== 'object' || Array.isArray(direct)) return null;
  return Object.entries(direct).map(([peer, d]) => ({id: peer, rttMs: Number.isFinite(d?.rttMs) ? d.rttMs : null, direct: d || {}}));
}
// What member `id` reports about its punched peers, best source first.
export function directPeers(id, edgeId = '') {
  if (edgeId && linkLive.id === edgeId && linkLive.peers[id]) return linkLive.peers[id];
  return summaryPeers(id) ?? peerLive[id];
}

// Installs run while POST /members is open; poll fast meanwhile.
let installs = 0;
export async function install(serverIds) {
  installs++; mesh.installing = true;
  try { return await act(() => MeshApi.addMembers(serverIds)); }
  finally { installs--; mesh.installing = installs > 0; }
}

// Members whose mesh service is being put back: {serverId: true}, here so a panel opened again still shows it.
export const reinstalling = reactive({});
// Asks, then reinstalls the mesh service of a member that lost it. The draft is not touched: the reply
// is taken in like a poll, so edits not yet saved stay. '' when done or cancelled, else the reason it was not.
export async function reinstallMesh(m) {
  try {
    await ElMessageBox.confirm(`会在「${m.name}」上重新安装组网服务，并签发一个新版本让其他成员更新它的密钥；你的草稿不受影响。`, '重新安装组网服务',
      {confirmButtonText: '重新安装', cancelButtonText: '取消', type: 'warning'});
  } catch { return ''; }
  const id = m.serverId;
  reinstalling[id] = true; installs++; mesh.installing = true;
  try {
    adopt(await MeshApi.reinstall(id));
    ElMessage.success(`已在「${m.name}」上重新安装组网服务，正在把新密钥下发给其他成员`);
    return '';
  } catch (e) {
    void refresh();
    return reinstallErrorText(e?.response?.data?.error) || meshError(e);
  } finally {
    delete reinstalling[id]; installs--; mesh.installing = installs > 0;
  }
}

// Marks a member for removal after asking; it leaves the mesh when applied.
export async function removeFromMesh(m) {
  try {
    await ElMessageBox.confirm(`应用后「${m.name}」将无法访问网格，机器上的组网服务会被卸载。`, '从组网移除', {confirmButtonText: '移除', cancelButtonText: '取消', type: 'warning'});
  } catch { return false; }
  const result = await act(() => MeshApi.removeMember(m.serverId));
  // One never applied leaves the draft at once.
  const gone = result && !(mesh.data?.members || []).some(x => x.serverId === m.serverId);
  if (result) ElMessage.success(gone ? '已移出组网，机器上的组网服务会被卸载' : '已标记为待移除，应用后生效');
  return Boolean(result);
}

// Apply, discard, rollback, add and remove change the draft on the server; read it back.
// Edits the server rejected are dropped with it.
// Polls go on while it runs (an install is followed by them); only those out when it starts or ends are dropped.
export async function act(call) {
  mesh.error = '';
  revision++;
  try {
    if (saving) await saving;
    const data = await call();
    mesh.rejected = false;
    adopt(Array.isArray((data?.mesh || data)?.members) ? data : await MeshApi.state(), true);
    return data || {};
  } catch (e) {
    const errors = e?.response?.data?.errors;
    if (Array.isArray(errors) && errors.length) mesh.errors = errors;
    mesh.error = errors?.length ? `${errors.length} 处设置需要修改` : meshError(e);
    return null;
  } finally { revision++; }
}
