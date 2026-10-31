import { Api } from '../api.js';

export const instanceStatuses = Object.freeze({draft: '待部署', deploying: '部署中', needs_verification: '待检查', ready: '正常', unreachable: '无法连接', failed: '部署失败', archived: '已归档'});
export const eventTypes = Object.freeze({login_success: '登录成功', login_failure: '登录失败', health: '运行异常', recovery: '运行恢复'});
export const operationStatuses = Object.freeze({pending: '等待开始', running: '进行中', completed: '已完成', failed: '失败', needs_verification: '待检查', interrupted: '已中断'});
export function statusLabel(value) { return instanceStatuses[value] || value || '未知'; }
// Do not present a contradictory backend snapshot as healthy/usable. This is a
// display guard, not a replacement for the controller's authoritative verifier.
export function displayStatus(item) {
  if (item?.status === 'ready' && ['degraded', 'unhealthy', 'unreachable'].includes(item.healthStatus)) return item.healthStatus === 'unreachable' ? 'unreachable' : 'needs_verification';
  return item?.status;
}
export function statusTone(value) {
  return {ready: 'ok', deploying: 'warn', needs_verification: 'warn', draft: 'off', archived: 'off'}[value] || 'bad';
}
export function timeLabel(value) { return value ? new Date(Number(value)).toLocaleString('zh-CN', {hour12: false}) : '尚无记录'; }
export function relativeTime(value, now = Date.now()) {
  if (!value) return '';
  const seconds = Math.max(0, Math.round((now - Number(value)) / 1000));
  if (seconds < 60) return '刚刚';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return timeLabel(value);
}
export function ok(data) { if (!data || data.ok === false) throw new Error('request_failed'); return data; }

// Why the controller offers a gateway update (backend updateReasons).
const GATEWAY_UPDATE_REASONS = {
  build_outdated: '主控带来了新版本的网关程序。',
  build_unreported: '网关是旧版本，还不会报告自己的版本。',
  control_not_loopback: '更新后，网关的管理接口改为只在服务器本机监听。',
};
export function gatewayUpdateReasons(gateway) {
  return (gateway?.updateReasons || []).map(code => GATEWAY_UPDATE_REASONS[code]).filter(Boolean).join('');
}
// The last update's outcome, e.g. "上次自动更新失败（3 分钟前）：……".
export function gatewayLastUpdate(gateway, now = Date.now()) {
  const last = gateway?.lastUpdate;
  if (!last?.atMs) return '';
  const head = `上次${last.auto ? '自动' : ''}更新${last.ok ? '成功' : '失败'}（${relativeTime(last.atMs, now)}）`;
  return last.ok ? head : `${head}：${hasErrorText(last.error) ? errorText(last.error) : last.error || '原因未知'}`;
}
// What the instance page's gateway strip says next to its update button.
export function gatewayUpdateHint(gateway) {
  const last = gateway?.lastUpdate;
  if (gateway?.autoUpdate && last?.auto && !last.ok) return '自动更新失败';
  return gateway?.autoUpdate ? '主控会自动更新' : '';
}

const ERRORS = {
  gateway_not_enabled: '请先启用访问网关。',
  gateway_port_in_use: '这个端口已被占用，请换一个。',
  gateway_move_with_instances: '已有实例在使用访问网关，不能更换它的运行位置或端口。',
  gateway_install_failed: '网关安装失败，请检查服务器后重试。',
  gateway_start_failed: '网关已安装但服务启动失败，请查看服务器上 openbear-gateway 服务的日志。',
  gateway_needs_systemd: '这台服务器没有 systemd（例如主控运行在 Docker 容器中），无法安装网关服务。请选择一台使用 systemd 的服务器。',
  gateway_needs_useradd: '这台服务器缺少 useradd，无法创建网关的系统用户。',
  gateway_install_needs_root: '安装网关需要 root 权限：请使用 root 登录，或为该用户配置免密 sudo。',
  gateway_binary_missing: '缺少网关程序，请先构建网关或使用发行版安装包。',
  gateway_architecture_unsupported: '这台服务器的系统架构不受支持。',
  gateway_unreachable: '网关已安装，但主控连不上它，请检查端口与防火墙。',
  gateway_username_unavailable: '这个登录账号已被另一个正在使用的实例占用，请换一个账号。',
  gateway_instance_limit: '网关里的实例数量已到上限（1000 个）。',
  instance_workspace_in_use: '这个目录与本服务器其他实例的数据目录重叠（含已归档实例），或容器重名，请换一个专用目录。',
  gateway_address_unknown: '无法确定这台服务器的地址：它只装了节点，节点也没有上报外网 IP。',
  node_offline: '这台服务器的节点离线，又没有配置 SSH 登录。',
  gateway_node_unavailable: '网关所在服务器的节点暂时连不上。网关的管理接口只在本机监听，要经节点才能更新，节点恢复后再试。',
  gateway_node_upgrade_required: '网关所在服务器的节点版本太旧。网关的管理接口只在本机监听，要经节点才能更新：先在服务器页更新节点。',
  gateway_node_exec_disabled: '网关所在服务器的节点关闭了命令执行。网关的管理接口只在本机监听，要经节点才能更新：先开启节点的命令执行。',
  node_exec_disabled: '这台服务器的节点关闭了命令执行，又没有配置 SSH 登录。',
  node_connection_lost: '和节点的连接中断了，命令可能已经执行，请检查后再试。',
  server_ssh_unavailable: '这台服务器没有配置 SSH 登录。',
  server_connection_failed: '连接失败：请检查地址、端口、用户和认证信息。',
  server_connection_timeout: '连接超时：请检查地址和端口是否可达。',
  server_in_use: '这台服务器正在被网关或实例使用，不能删除。',
  server_not_found: '服务器不存在，请刷新。',
  local_server_fixed: '本机不能修改或删除。',
  server_name_required: '请填写服务器名称。',
  invalid_server_host: '服务器地址格式不正确。',
  invalid_server_user: '用户名格式不正确。',
  ssh_password_required: '请填写 SSH 密码。',
  ssh_key_reference_required: '请选择密钥库中的私钥。',
  ssh_key_missing: '所选私钥在密钥库中已不存在，请重新选择。',
  ssh_key_passphrase_incorrect: '私钥密码不正确，请选择正确的密码字段。',
  ssh_key_passphrase_missing: '找不到私钥密码所在的字段。',
  ssh_key_unsupported: '无法使用这把私钥。',
  ssh_private_key_invalid: '上传的文件不是可用的 SSH 私钥（或设有密码）。',
  ssh_key_vault_save_failed: '私钥保存到密钥库失败。',
  server_credential_required: '请提供登录凭证。',
  instance_port_in_use: '这个端口已被占用，请换一个或留空自动选择。',
  instance_primary_model_not_copied: '所选默认模型不在复制的渠道中。',
  no_free_port: '没有可用端口，请在“高级”中手动指定。',
  instance_address_required: '无法确定实例地址，请在“高级”中填写。',
  instance_server_immutable: '实例创建后不能更换运行位置。',
  instance_port_fixed_on_mesh: '这个实例放在组网上，端口不能直接改：组网规则会跟着改到新端口，实例本身却还在旧端口上。',
  instance_port_fixed: '这个实例的端口是部署时定下的，不能直接改：记录会改到新端口，运行中的实例却还在旧端口上。',
  initial_gateway_password_required: '请填写登录密码。',
  operation_running: '正在部署或维护中，请等待完成。',
  maintenance_scope_required: '请写明要做什么。',
  invalid_gateway_client_ip: '代理地址格式不正确，每行一个 IP 或网段。',
  invalid_server: '服务器信息不正确。',
  invalid_server_port: '端口不正确（1–65535）。',
  invalid_server_auth: '请选择登录方式。',
  invalid_port: '端口不正确（1–65535）。',
  ssh_key_reference_invalid: '请选择密钥库中的私钥。',
  ssh_client_unavailable: '主控所在机器缺少 ssh 客户端（ssh、ssh-keygen）。',
  command_unavailable: '主控所在机器无法启动所需的命令。',
  sudo_password_unavailable: '需要 sudo，但这台服务器没有保存可用的登录密码：请用 root 登录或配置免密 sudo。',
  instance_not_found: '实例不存在，请刷新。',
  instance_archived: '实例已归档。',
  instance_changed_after_confirmation: '实例在确认后又有变化，请刷新后重新操作。',
  stop_operation_before_archive: '正在部署或维护中，请先停止再归档。',
  gateway_revocation_pending: '访问网关的账号还在撤销中，请稍后再试。',
  gateway_not_configured: '还没有设置访问网关。',
  gateway_stop_failed: '网关停止失败，请检查服务器后重试。',
  gateway_initial_account_required: '请先设置网关的登录账号和密码。',
  gateway_sync_required_before_new_deployment: '请先同步访问网关，再部署。',
  instance_address_pending: '实例地址还没有确定，部署完成后再同步网关。',
  invalid_image: '镜像名称不正确。',
  web_dist_missing: '主控缺少前端构建产物，无法按源码部署。',
  invalid_initial_config: '初始配置不正确，请检查复制的模型渠道。',
  unknown_model: '所选模型不存在，请重新选择。',
  child_unreachable: '连不上这个实例。',
  child_invalid_response: '实例返回的数据异常。',
  controller_only: '只有主控能执行这个操作。',
  protected_storage_unavailable: '加密存储暂时不可用，请稍后重试。',
  instance_mesh_required: '实例和访问网关不在同一台服务器：两台都加入组网后才能这样部署。',
  dedicated_volume_required: '数据目录不能是系统目录或共用目录，请换一个专用目录。',
  controller_directory_volume_forbidden: '数据目录不能是主控自己的项目或数据目录，也不能是它们的上级目录。',
  memory_limit_too_small: '内存上限至少 256 MB，留空表示不限。',
  gateway_control_port_unavailable: '找不到给网关管理用的空闲端口。',
  node_http_required: '这台服务器的节点不在线或版本太旧，无法改为只在本机监听。',
  instance_address_fixed: '这个实例手动填写了实例地址，请在设置中修改。',
  instance_boot_unit_failed: '无法在服务器上设置开机启动实例。',
  instance_reconfigure_failed: '更新实例容器失败，请检查服务器后重试。',
  instance_rotation_failed: '更换实例密钥失败，稍后会自动重试。',
  instance_image_unknown: '找不到已部署实例的镜像信息，旧密钥保持不变。请在实例设置中补填实际镜像，或恢复部署目录中的 compose.json 后重试。',
  instance_gateway_auth_required: '这个实例不使用访问网关登录。',
  gateway_invalid_response: '网关返回的数据异常。',
  child_response_too_large: '实例返回的数据过大。',
  gateway_update_running: '访问网关正在更新，请稍后再看。',
  gateway_update_failed: '网关更新失败，请检查服务器后重试。',
  gateway_update_interrupted: '主控关闭中断了网关更新，远端结果待确认；恢复后请检查网关状态。',
  invalid_gateway_auto_update: '自动更新设置无效，请刷新后重试。',
};
// The SSH host and user the controller accepts, as input `pattern` attributes;
// surrounding spaces are trimmed before saving.
export const HOST_PATTERN = '\\s*[A-Za-z0-9][A-Za-z0-9.:\\-]*\\s*', HOST_TITLE = 'IP 或域名，只能包含字母、数字、. : -';
export const USER_PATTERN = '\\s*[A-Za-z0-9_][A-Za-z0-9_.\\-]*\\s*', USER_TITLE = '用户名只能包含字母、数字、_ . -';
export function errorText(code) { return safeError({response: {data: {error: code}}}); }
export function hasErrorText(code) { return Object.hasOwn(ERRORS, code); }
// Do not echo raw backend text: it may include submitted passwords or keys.
export function safeError(error) {
  const code = error?.response?.data?.error;
  if (code && ERRORS[code]) return ERRORS[code];
  const status = error?.response?.status;
  if (status === 401) return '登录已过期，请重新登录。';
  if (status === 403) return '此操作未获授权。';
  if (status === 404) return '资源不存在，请刷新。';
  if (status === 409) return '当前状态有冲突，请刷新后再试。';
  if ([400, 422].includes(status)) return '填写的内容未通过校验，请检查后重试。';
  if (status === 429) return '操作过于频繁，请稍后再试。';
  if (status === 502 || status === 503) return '无法连接目标，请稍后重试。';
  return '操作未确认成功，请刷新查看实际状态。';
}
export function publicLink(value) {
  try { const url = new URL(value); return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : ''; } catch { return ''; }
}
export function parseObject(value) {
  const result = JSON.parse(value || '{}');
  if (!result || Array.isArray(result) || typeof result !== 'object') throw new Error('object_required');
  return result;
}
// Readable, unambiguous and long enough; the user can still type their own.
export function generatePassword(random = globalThis.crypto) {
  const chars = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  const bytes = new Uint32Array(12);
  random.getRandomValues(bytes);
  const text = Array.from(bytes, n => chars[n % chars.length]).join('');
  return `${text.slice(0, 4)}-${text.slice(4, 8)}-${text.slice(8)}`;
}
// Advice only: any non-empty password is accepted.
export function passwordHint(value) {
  if (!value) return '';
  const kinds = [/[a-z]/, /[A-Z]/, /\d/, /[^a-zA-Z\d]/].filter(re => re.test(value)).length;
  if (value.length < 8) return '密码较短，容易被猜到；建议至少 8 位。';
  if (value.length < 12 && kinds < 3) return '建议 12 位以上，或混合大小写字母、数字和符号。';
  return '';
}
export function serverLabel(server) {
  if (!server) return '';
  if (server.local) return '本机（主控所在服务器）';
  if (nodeReady(server)) return `${server.name} · 节点通道`;
  if (server.auth === 'none') return `${server.name} · ${server.node?.online ? '节点未开启命令执行' : '节点离线'}`;
  return `${server.name} · ${server.host}${server.status === 'unreachable' ? '（无法连接）' : ''}`;
}
// Instance and gateway commands go through an online node first, SSH otherwise.
export function nodeReady(server) { return Boolean(server?.node?.online && server.node.exec); }
// usedBy lists instance names, and the gateway under this fixed name.
const GATEWAY_USE = '访问网关';
export function serverUse(server) {
  const used = server?.usedBy || [];
  return {gateway: used.includes(GATEWAY_USE), instances: used.filter(name => name !== GATEWAY_USE)};
}
// Why commands cannot reach the server now; '' when they can.
export function serverUnavailable(server) {
  if (!server || server.local || server.auth !== 'none' || nodeReady(server)) return '';
  return server.node?.online ? '节点未开启命令执行' : '节点离线';
}
export function serverChannel(server) {
  if (!server || server.local || serverUnavailable(server)) return '';
  if (nodeReady(server)) return '节点';
  return server.status === 'unreachable' ? 'SSH 无法连接' : 'SSH';
}
// Region, the local host's own name and the order come from the servers page list.
export function withServerPage(items, page) {
  const known = new Map((page || []).map((server, index) => [server.id, {server, index}]));
  return items.map(item => {
    const match = known.get(item.id);
    return match ? {...item, name: match.server.name || item.name, region: match.server.region || '', order: match.index} : item;
  }).sort((a, b) => (a.order ?? Infinity) - (b.order ?? Infinity));
}
export function defaultAccessUrl(ip, port) { return ip && port ? `http://${ip}:${port}` : ''; }
// The address the user typed differs from server-IP:port, so they must map it.
export function needsMapping(url, ip, port) {
  try {
    const parsed = new URL(url);
    const effective = parsed.port || (parsed.protocol === 'https:' ? '443' : '80');
    return Boolean(ip && port) && (parsed.hostname !== ip || effective !== String(port));
  } catch { return false; }
}
// The target is captured at construction. No caller can switch host, send headers,
// or use a child path to call a parent endpoint. Backend applies the authoritative
// method/path allowlist as well; this guard is not a replacement for authorization.
export function childClient(instanceId, api = Api) {
  const id = String(instanceId);
  return async (method, path, body) => {
    if (!/^\/api\/(?:memory\/entries(?:[/?]|$)|channels(?:[/?]|$)|skills(?:[/?]|$)|mcp(?:[/?]|$)|settings(?:[/?]|$)|managed\/v1\/(?:config|status|jobs|files)(?:[/?]|$))/.test(path) || /(?:\.\.|\\|#|%2e|%5c)/i.test(path)) throw new Error('invalid_child_path');
    const response = ok(await api.instanceRequest(id, {method, path, ...(body === undefined ? {} : {body})}));
    if (!Number.isInteger(response.status) || response.status < 200 || response.status >= 300 || response.data?.ok === false) {
      const error = new Error('child_request_failed');
      error.response = {status: response.status};
      if (response.data?.error === 'mcp_exposure_confirmation_required') error.mcpChallenge = response.data;
      throw error;
    }
    return response.data;
  };
}
export function initialModelConfig(form) {
  const ids = form.modelIds.split(/[,\n]+/).map(x => x.trim()).filter(Boolean);
  if (!form.providerName.trim() || !form.baseUrl.trim() || !ids.length) throw new Error('model_required');
  return {models: {primary: `${form.providerName.trim()}/${ids[0]}`, providers: {[form.providerName.trim()]: {
    baseUrl: form.baseUrl.trim(), apiKey: form.apiKey, protocol: form.protocol, models: ids.map(id => ({id, name: id})),
  }}}};
}
