import { hasErrorText, safeError as instanceError } from '../instances/instanceHelpers.js';

export { ensureFlagFont, flag } from './flags.js';
import { SERVICE_ICONS } from './serviceIcons.js';
const regionNames = typeof Intl !== 'undefined' && Intl.DisplayNames ? new Intl.DisplayNames(['zh-CN'], {type: 'region'}) : null;
export function regionName(code) {
  try { return code ? regionNames?.of(code.toUpperCase()) || code : ''; } catch { return code || ''; }
}

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
export function bytes(value, digits = 1) {
  let n = Number(value) || 0, i = 0;
  while (n >= 1024 && i < UNITS.length - 1) { n /= 1024; i++; }
  return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(digits)} ${UNITS[i]}`;
}
export function rate(value) { return `${bytes(value)}/s`; }
export function percent(used, total) { return total > 0 ? Math.min(100, Math.max(0, used / total * 100)) : 0; }
export function uptime(seconds) {
  const s = Number(seconds) || 0;
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d} 天 ${h} 小时` : h ? `${h} 小时 ${m} 分` : `${m} 分钟`;
}
export function tone(value) { return value >= 90 ? 'bad' : value >= 75 ? 'warn' : 'ok'; }

const CYCLES = {[-1]: '一次性', 0: '未设置', 30: '月付', 31: '月付', 90: '季付', 91: '季付', 92: '季付', 180: '半年付', 181: '半年付', 182: '半年付', 183: '半年付', 184: '半年付', 365: '年付', 366: '年付', 730: '两年付', 1095: '三年付', 1825: '五年付'};
export const cycleOptions = [[30, '月付'], [90, '季付'], [184, '半年付'], [365, '年付'], [730, '两年付'], [1095, '三年付'], [-1, '一次性'], [0, '未设置']];
export function cycleLabel(cycle) { return CYCLES[cycle] || (cycle > 0 ? `${cycle} 天` : '未设置'); }
// The servers' time zone (设置 → 时区): "today" for an expiry is the reminders' today, not the browser's.
let zone = '';
export function setServerZone(value) { zone = value || ''; }
function zonedToday(now) {
  try {
    if (zone) return new Intl.DateTimeFormat('en-CA', {timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit'}).format(now);
  } catch { /* an unknown zone: the browser's */ }
  const pad = n => String(n).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}
export function daysLeft(date, now = new Date()) {
  if (!date) return null;
  return Math.round((Date.parse(`${date}T00:00:00Z`) - Date.parse(`${zonedToday(now)}T00:00:00Z`)) / 86400000);
}
export function expiryText(billing, now) {
  if (!billing || billing.cycle === -1) return billing?.cycle === -1 ? '一次性付费' : '';
  const days = daysLeft(billing.expiresAt, now);
  if (days === null) return '';
  if (days < 0) return `已过期 ${-days} 天`;
  if (days === 0) return '今天到期';
  return `${days} 天后到期`;
}
export function expiryTone(billing, now) {
  const days = billing?.cycle === -1 ? null : daysLeft(billing?.expiresAt, now);
  return days === null ? 'off' : days <= 3 ? 'bad' : days <= 10 ? 'warn' : 'ok';
}
export function priceText(billing) {
  if (!billing || !billing.price) return billing?.cycle === -1 ? '一次性' : '';
  return `${billing.currency || ''}${billing.price}${billing.cycle > 0 ? ' / ' + cycleLabel(billing.cycle).replace('付', '') : billing.cycle === -1 ? ' 一次性' : ''}`;
}
const TERMINAL_REASONS = {exited: '会话已结束', closed: '会话已关闭', idle: '30 分钟没有输入也没有输出，已自动断开', node_disconnected: '节点连接断开',
  node_offline: '节点不在线', capability_disabled: '这台服务器的节点关闭了终端', node_not_enrolled: '这台服务器还没有安装节点',
  open_timeout: '节点没有响应', start_failed: '无法在服务器上启动 shell', too_many_terminals: '这台服务器打开的终端太多',
  input_overflow: '服务器上的程序长时间不读取输入，会话已关闭', protocol_error: '节点数据异常，会话已关闭', open_failed: '节点没能打开终端'};
export function terminalReason(reason) { return TERMINAL_REASONS[reason] || '连接已断开'; }
const DESKTOP_REASONS = {
  ...TERMINAL_REASONS,
  connect_failed: '无法连接本机远程桌面端口，请确认已开启远程桌面（Windows 3389）或屏幕共享（macOS 5900）',
  capability_disabled: '这台服务器的节点关闭了命令执行，无法转发桌面连接',
  node_upgrade_required: '这台服务器的节点版本过旧，请先更新节点',
  auth_failed: '桌面登录失败，请检查用户名和密码',
  wrong_password: '密码不正确，请重新输入',
  logon_failure: '登录失败，请检查用户名和密码',
  access_denied: '账户没有远程桌面登录权限',
  tls_timeout: '远程桌面 TLS 握手超时',
};
export function desktopReason(reason) { return DESKTOP_REASONS[reason] || reason || '桌面连接已断开'; }
// [type, name, what it counts]; a profile without a type counts the larger direction, as it always did.
export const trafficTypes = [['sum', '双向计费', '上下行合计'], ['up', '单向：上行', ''], ['down', '单向：下行', ''], ['max', '取大', '上下行较大的一方'], ['min', '取小', '上下行较小的一方']];
export const trafficTypeText = type => { const [, name, hint] = trafficTypes.find(([k]) => k === (type || 'max')) || []; return name ? (hint && type !== 'sum' ? `${name}（${hint}）` : name) : ''; };
export const trafficModes = [['billing', '按账单日重置'], ['fixed', '每月固定日期'], ['metered', '按流量计费'], ['unlimited', '不限流量']];
// Profiles saved before modes existed: a reset day meant a fixed date.
export function trafficMode(traffic) { return traffic?.mode || (traffic?.resetDay ? 'fixed' : 'billing'); }
// Day of month a billing-date period starts on, as the controller derives it.
export function billingResetDay(billing) {
  const anchor = billing?.expiresAt || billing?.startedAt;
  return anchor ? Number(anchor.slice(8, 10)) : 1;
}

// The controller's built-in mesh access point (settings.meshAccess): members can reach the controller over the mesh.
export const MESH_ACCESS_ID = 'ap-mesh';
const MESH_ACCESS_REASONS = {controller_not_member: '主控本机还没加入组网', controller_no_address: '主控本机还没分到组网地址', controller_url_requires_https: '该组网地址不允许明文 HTTP 管理连接；数据面可用，请另配 HTTPS 接入点'};
const SERVER_MESH_REASONS = {not_member: '这台不在组网里', node_outdated: '这台的节点版本太旧，先更新节点', local: '本机不经组网连接'};
// Why the mesh access point cannot be used ('' when it can): the controller's side, then (with eligible given)
// the server's, by its access.meshReason.
export function meshAccessReason(mesh, eligible = true, reason = '') {
  if (!mesh) return '';
  if (!mesh.available) return MESH_ACCESS_REASONS[mesh.reason] || '主控本机暂时不能经组网连接';
  return eligible ? '' : SERVER_MESH_REASONS[reason] || '这台不在组网里';
}

const ERRORS = {
  node_url_required: '请先在「设置」里填写节点接入地址。',
  invalid_node_url: '接入地址格式不正确，应为 https://域名 这样的地址。',
  node_url_requires_https: '公网地址必须使用 https；只有内网或本机地址可以用 http。',
  node_binary_missing: '没有可用的节点程序：本地没有构建，也没能从 GitHub 获取。请在「设置」里检查节点程序。',
  node_download_failed: '没能从 GitHub 下载这台服务器所需的节点程序，请稍后重试。',
  node_install_running: '正在安装，请等待完成。',
  install_command_failed: '安装命令执行失败，请查看下方的安装输出。',
  install_timeout: '安装超时（5 分钟），请检查服务器能否访问节点接入地址。',
  install_failed: '安装没有完成，请检查服务器状态后重试。',
  machine_has_node: '这台机器已经作为另一台服务器接入了：先删除那台，或直接在它上面重新安装。',
  machine_has_other_node: '这台机器上已经装有其他节点（可能属于别的主控），确认要替换时用“仍然覆盖安装”。',
  node_not_enrolled: '这台服务器还没有安装节点。',
  node_offline: '节点当前不在线。',
  server_ssh_unavailable: '这台服务器没有登记 SSH 连接。',
  invalid_server_date: '日期格式不正确。',
  invalid_server_region: '地区应为两位国家代码，例如 SG。',
  invalid_billing_cycle: '计费周期不正确。',
  invalid_billing_price: '价格不正确。',
  invalid_traffic_limit: '流量额度不正确。',
  invalid_traffic_reset_day: '请填写每月几号重置（1–31）。',
  invalid_timezone: '时区名称不正确，例如 Asia/Shanghai。',
  local_server_fixed: '本机不能删除。',
  command_name_required: '请填写命令名称。',
  command_text_required: '请填写命令内容。',
  capability_disabled: '这台服务器的节点关闭了命令执行（安装时用了 --no-exec）。',
  node_update_needs_exec: '这台服务器的节点关闭了命令执行，不能在线更新，请在服务器上重新运行安装命令。',
  node_connection_lost: '和节点的连接中断了，操作结果未知，请刷新后确认。',
  node_busy: '节点正在执行太多任务，请稍后再试。',
  job_stop_unconfirmed: '尚未确认后台任务已停止。',
  server_work_stop_failed: '任务或评测未能全部停止，已保留服务器和节点信息。',
  node_upgrade_required: '节点版本过旧，升级节点后可用。',
  invalid_probe: '测试参数不正确。',
  mesh_access_requires_member: '这台服务器不在组网里，不能选组网接入。先在组网页把它加入网格并应用，或换一个接入点。',
  mesh_access_node_outdated: '这台服务器的节点版本太旧，还不支持经组网连主控。先更新节点，或换一个接入点。',
  probe_busy: '这台服务器上已经有 4 个测试在运行，请等其中一个结束后再试。',
  invalid_browser_settings: '浏览器设置不正确。',
  invalid_browser_update: '浏览器更新参数不正确。',
  invalid_browser_endpoint: '浏览器地址应为 http://、https://、ws:// 或 wss:// 开头的调试（CDP）地址。',
  config_write_failed: '节点没能写入配置文件。',
  browser_not_started: '节点的浏览器还没有启动。',
  browser_local_default: '本机不需要开启节点浏览器：浏览器工具默认用的就是设置里配置的浏览器。',
  checksum_mismatch: '下载的节点程序校验失败，没有替换。',
  download_failed: '节点没能从主控下载新版本，请检查它到接入点的连接后重试。',
  new_binary_does_not_run: '新版本在这台服务器上无法运行，没有替换。',
  wrong_platform: '没有适合这台服务器架构的节点程序。',
  file_exists: '同名文件已存在。',
  invalid_file_path: '路径不正确。',
  invalid_file_paths: '请选择要操作的文件（一次最多 200 个）。',
  invalid_file_name: '名称不能为空，也不能包含斜杠等文件名不允许的字符。',
  invalid_file_mode: '权限应为 3 到 4 位八进制数字，例如 644 或 0755。',
  protected_path: '这是系统目录或节点自身的文件，不允许这样操作。',
  compress_one_folder: '只能压缩同一目录下的文件。',
  invalid_archive_format: '不支持这种压缩格式。',
  not_an_archive: '这不是支持的压缩包（tar、tar.gz、tar.xz、tar.bz2、tar.zst、zip）。',
  archive_tool_missing: '服务器上缺少压缩工具：Linux 处理 zip 需要 zip、unzip 或 python3，tar.xz 需要 xz，tar.bz2 需要 bzip2，tar.zst 需要 zstd（也可以改用 tar.gz）；Windows 需要系统自带的 tar.exe（Windows 10 1803 及以后）。',
  chmod_unsupported: 'Windows 不使用这种权限设置。',
  file_in_use: '文件正被其他程序占用。',
  file_op_timeout: '操作超时（30 分钟），可能仍在服务器上进行。',
  file_op_failed: '操作失败。',
  file_job_not_found: '这次操作的结果已过期，请刷新目录查看。',
  not_found: '文件或目录不存在。',
  permission_denied: '没有权限。',
  is_directory: '这是一个目录。',
  not_a_directory: '路径中有一段不是目录。',
  not_regular_file: '这不是普通文件，不能下载。',
  no_space: '服务器磁盘空间不足。',
  read_only_filesystem: '目标文件系统是只读的。',
  billing_cycle_required: '请先在编辑里设置计费周期（一次性付费不需要续费）。',
  invalid_traffic_calibration: '流量数值不正确，剩余不能超过额度。',
  invalid_retention: '保留期限不正确：分钟级至少 7 天。',
  retention_hours_shorter: '小时级数据的保留期限不能短于分钟级。',
  retention_hours_too_short: '小时级数据至少保留 31 天，或设为永久。',
  invalid_latency_interval: '探测间隔应为 3–600 秒的整数。',
  invalid_latency_province: '观测地不在节点目录里。',
  invalid_latency_targets: '延迟目标不正确：从列表里选，或填 主机:端口，不能是本机地址，最多 16 个。',
  invalid_latency_settings: '延迟监测设置不正确。',
  invalid_latency_custom: '自定义地址不正确：填名称、域名或 IPv4 地址和端口，不能是本机地址，最多 32 个。',
  traffic_limit_required: '没有设置流量额度，只能填已用多少。',
  node_required: '这台服务器还没有安装节点。',
  invalid_reminder_time: '提醒时间格式应为 HH:MM，例如 10:00。',
  invalid_expiryDays: '到期提醒天数应为 0–90 的整数，最多 10 个。',
  invalid_trafficPercents: '流量提醒应为 1–100 的整数，最多 10 个。',
  exec_job_not_found: '这次批量执行的结果已过期。',
  invalid_exec_targets: '请选择要执行的服务器（一次最多 100 台）。',
  invalid_exec_timeout: '超时时间不正确（1 秒到 1 小时）。',
  exec_failed: '执行失败。',
  new_url_unreachable: '节点连不上新地址，已保持原地址。',
  invalid_url: '接入点地址不正确，节点没有采用。',
  invalid_access_apply: '接入点的生效方式不正确。',
  access_point_name_required: '请填写接入点名称（最多 30 个字）。',
  access_point_url_required: '请填写接入点地址。',
  invalid_access_points: '接入点设置不正确（最多 10 个）。',
  access_tls_invalid: '证书无效：检查证书是否包含这个域名、有没有过期。',
  access_unreachable: '连不上这个地址：检查域名解析、端口和防火墙。',
  access_path_not_forwarded: '这个地址没有转发到 OpenBear：反向代理需要转发 /api/server-nodes/。',
  access_other_controller: '这个地址连到的是另一台 OpenBear。',
  install_check_failed: '服务器检查没有通过，详情见下方。',
  install_download_failed: '服务器下载节点程序失败：它可能连不上所选的接入点。',
  install_enroll_failed: '节点注册失败，详情见下方。',
  install_service_failed: '启动节点服务失败，详情见下方。',
  node_not_connected: '节点已经装好，但 30 秒内没有连上：服务器可能连不上所选的接入点，可以换一个接入点再试。',
  registration_code_name_required: '请给注册码起个名字。',
  registration_code_not_found: '注册码不存在。',
  ssh_connect_failed: 'SSH 连接失败。',
  server_name_required: '请填写名称。',
  invalid_server_host: '地址格式不正确。',
  invalid_server_port: '端口不正确。',
  invalid_batch: '每行填一台服务器，格式是“名称 地址 端口”，一次最多 50 台。',
  invalid_server_order: '服务器列表已变化，请关闭后重新调整。',
  invalid_traffic_price: '流量单价不正确。',
  invalid_traffic_mode: '流量方式不正确。',
  invalid_traffic_type: '流量计算方式不正确。',
  invalid_server_alias: '别名太长（最多 60 个字）。',
  invalid_server_group: '分组名称太长（最多 40 个字）。',
  invalid_server_provider: '服务商名称太长（最多 60 个字）。',
  invalid_server_provider_url: '商家网址要以 http:// 或 https:// 开头，最多 300 个字。',
  invalid_server_tags: '标签最多 10 个，每个最多 20 个字。',
  invalid_server_remark: '备注太长（最多 2000 个字）。',
  invalid_server_sort: '排序值不正确。',
  invalid_billing_currency: '币种太长（最多 8 个字符）。',
  invalid_server_access: '接入点选择不正确，请刷新后重新选择。',
  invalid_server_billing: '账单设置不正确。',
  invalid_server_traffic: '流量设置不正确。',
  invalid_server_profile: '服务器资料不正确。',
  invalid_server_settings: '设置内容不正确。',
  invalid_reminders: '提醒设置不正确。',
  invalid_registration_code: '注册码设置不正确（有效期 0–3650 天，次数 0–10000）。',
  command_not_found: '这条命令已被删除，请刷新。',
  invalid_command: '命令内容不正确。',
  invalid_file_op: '不支持这种文件操作。',
  body_too_large: '内容太大：上传的文件最大 4 GB。',
  public_key_in_use: '这个节点的密钥已被另一台服务器使用，不能重复注册。',
  upload_offset_mismatch: '上传中断了，请重新上传。',
  upload_out_of_order: '上传中断了，请重新上传。',
  io_error: '服务器读写文件时出错，请检查磁盘后重试。',
  browser_installing: '浏览器还在下载，请等下载完成后再试。',
  unsupported_method: '这台服务器的节点版本太旧，不支持这个操作，请先更新节点。',
  internal_error: '节点内部出错，请稍后重试。',
  storage_unavailable: '主控的数据库暂时不可用，请稍后重试。',
  rate_limited: '操作过于频繁，请稍后再试。',
  batch_not_found: '这次批量安装的进度已过期，请在服务器列表里查看结果。',
  invalid_command_order: '命令列表已变化，请刷新后重新调整。',
  invalid_exec: '执行参数不正确。',
  invalid_range: '时间范围不正确。',
  node_release_cache_unset: '主控还没有准备好节点程序目录，请稍后再检查。',
  invalid_params: '节点不接受这个请求，可能需要先更新节点。',
  invalid_mode: '权限应为 3 到 4 位八进制数字，例如 644 或 0755。',
  unsupported_platform: '节点在这个系统上不支持这个操作。',
  executable_unknown: '节点找不到自己的程序文件，无法在线更新，请在服务器上重新运行安装命令。',
  node_update_required: '这台服务器的节点版本太旧，不支持这个操作，请先更新节点。',
  unlock_check_disabled: '解锁检测已在服务器设置中关闭。',
  route_check_disabled: '三网回程已在服务器设置中关闭。',
  route_no_ipv4: '这台服务器没有 IPv4 地址，三网回程只测 IPv4。',
  check_not_for_china: '国内服务器不做解锁检测和三网回程。',
  check_not_for_front: '前置机不做解锁检测和三网回程，请到出口服务器上检测。',
  invalid_front: '前置机设置不正确：请选择 1–2 台出口服务器；主控本机不能设为前置机。',
  invalid_front_address: '出口地址不正确：请填前置机能访问的出口 IP 或域名，不带协议和端口。',
  invalid_front_exit: '出口服务器不可用，不能选择主控本机或前置机自身，请刷新后重新选择。',
  front_exit_duplicate: '两个出口选了同一台服务器，请换一台或去掉备用出口。',
  front_exit_is_front: '所选出口本身是前置机，不能再作为出口，请选择普通服务器。',
  front_is_exit: '这台服务器正在供其他前置机作为出口，请先为那些前置机更换出口，再把它设为前置机。',
  front_exit_not_ready: '出口还没有准备好前置接入服务，请确认出口节点在线、版本已更新，并检查前置接入端口后重试。',
  front_exit_offline: '出口节点不在线或不支持前置接入，请先让出口上线并更新节点，再重试。',
  front_serve_failed: '出口没能启用前置接入服务，请检查出口节点和端口是否被占用后重试。',
  front_port_fronts_offline: '有前置机不在线，收不到新的接入地址。请先让所有前置机上线，再修改前置接入端口。',
  invalid_front_port: '前置接入端口不正确，请填写 1–65535 的整数。',
  invalid_httpbl_key: 'http:BL 密钥格式不正确，请填写 12 位小写字母的密钥；留空可清除。',
  secret_save_failed: 'IP 情报凭据没能保存到凭据库，请检查凭据库是否可用后重试。',
  no_public_ipv4: '这台服务器没有可用的公网 IPv4 地址，请确认节点已上报公网 IPv4 后再扫描端口。',
  invalid_ipcheck_days: 'IP 质量检测间隔不正确，请选择 1、7、15 或 30 天。',
  invalid_unit: '流量统计单位不正确，请选择按小时、按日或按月。',
  node_update_running: '节点正在更新，请等本次更新结束后再试。',
  command_required: '还没有填写要运行的命令，请填写后再启动任务。',
  pattern_timeout: '日志匹配耗时过长，已停止匹配，请简化正则表达式后重试。',
  invalid_edits: '替换内容不正确：每处要有原文和新内容，一次最多 100 处，合计不超过 4 MB。',
  edit_not_applied: '有替换没能匹配（没找到、出现多次或互相重叠），文件没有修改。',
  not_text: '这不是 UTF-8 文本文件，不能按文本替换。',
  file_too_large: '文件太大（超过 4 MB）。',
  file_changed: '修改期间文件被其他程序改动了，没有写入，请重新读取后再试。',
  file_size_unknown: '节点没有返回这个文件的大小，无法下载。',
  invalid_job: '后台任务的参数不正确。',
  invalid_cwd: '工作目录不存在或不是绝对路径。',
  too_many_jobs: '这台服务器上运行中的后台任务太多了，请先结束一些。',
  start_failed: '节点没能启动这个任务。',
  job_not_found: '这个后台任务不存在，可能已被清理。',
  job_unreadable: '这个后台任务的记录损坏了，读取不了。',
  job_id_required: '请指定后台任务。',
  invalid_pattern: '等待条件不是有效的正则表达式。',
  invalid_limit: '读取长度不正确。',
  invalid_offset: '读取位置不正确。',
  invalid_timeout: '等待时间不正确。',
  ssh_fallback_stdin_unsupported: '节点离线时改走 SSH 执行，这种方式不能附带标准输入。',
  ssh_connection_failed: 'SSH 连接失败，命令没有执行。',
  ssh_result_unknown: 'SSH 连接中断，命令结果未知，可能已经执行。',
  bench_unknown_test: '没有这个评测项目。',
  bench_invalid_option: '评测参数不正确。',
  bench_duplicate_test: '同一个评测项目选了两次。',
  invalid_sort: '不支持这种排序。',
  unsupported_sort: '这个系统暂不支持这种排序。',
  invalid_pid: '这个进程不能结束。',
  process_not_found: '进程已经不在了。',
  bench_platform_unsupported: '评测目前只支持 Linux（amd64、arm64）节点。',
  bench_openwrt_unsupported: 'OpenWrt 上只能运行 Service Unlock：其余评测脚本不支持 OpenWrt，Geekbench 也无法在它的 musl 系统上运行。',
  bench_exec_disabled: '这台服务器的节点关闭了命令执行，不能运行评测。',
  platform_unsupported: '评测目前只支持 Linux（amd64、arm64）节点。',
  bench_run_not_found: '这条评测记录不存在，可能已被删除。',
  bench_run_active: '评测还没结束，先停止再删除。',
  bench_no_report: '脚本运行结束了，但没有产生报告，可以看看运行日志。',
  bench_script_failed: '评测脚本未正常完成，请查看退出码和运行日志；已有输出仍保留。',
  bench_package_corrupt: '节点上的评测包文件不完整，下次运行时会重新推送。',
  bench_job_lost: '节点上找不到这次评测的任务记录，结果没能收回。',
  bench_timeout: '评测超过时限未完成，已自动停止。',
  bench_internal: '评测出错了，可以看看运行日志。',
  package_missing: '主控缺少这个评测包，可能需要更新主控。',
  package_invalid: '主控上的评测包文件和清单对不上，没有使用。',
  download_failed: '主控没能下载评测用到的程序，请检查主控能否访问下载地址。',
  checksum_mismatch: '下载的评测程序校验不通过，已丢弃。',
};
// linux, darwin or windows: what the node reports, else what SSH found.
export const platformOf = item => item?.node?.info?.platform || item?.ssh?.platform || 'linux';
export const hasDesktop = item => { const p = platformOf(item); return p === 'darwin' || p === 'windows'; };
export const canDesktop = item => hasDesktop(item) && Boolean(item?.node && item?.live && item.node.caps?.exec !== false);

// Per address family the node has: its ASN, operator and country, when the controller looked up
// this very address, and the country Google serves it as (CN: sent to google.cn) by the unlock check.
// region: the country the user set for the server, which stands for GeoIP's.
export function addressNetworks(node, region = '') {
  const info = node?.info || {}, net = node?.network || {}, checked = node?.unlock || {};
  return [[4, info.ipv4, 0, net.v4], [6, info.ipv6, 1, net.v6]]
    .filter(([, ip]) => ip)
    .map(([family, ip, index, found]) => {
      const known = net.ips?.[index] === ip && found ? found : null;
      const google = checked.ips?.[index] === ip ? checked[`v${family}`]?.google : null;
      return {family, ip, asn: known?.asn || null, org: known?.org || '', country: region || known?.country || '',
        google: google?.state === 'ok' ? google.region || '' : ''};
    });
}

const UNLOCK_STATES = {ok: '可用', limited: '部分可用', blocked: '不可用', error: '检测失败'};
const UNLOCK_NOTES = {originals: '仅自制剧', api: '网页被 Cloudflare 拦下，按 API 判断', datacenter: '拒绝机房 IP', challenge: '被 Cloudflare 人机验证拦下',
  timeout: '请求超时', failed: '连接失败', http: '返回异常', empty: '返回为空', unclear: '无法判断'};

// A corner mark when a service serves the address as another country than it is in; CN, in red,
// also where the address's own country is unknown.
function unlockMark(verdict, home) {
  const where = verdict.state !== 'error' ? verdict.region : '';
  if (!where || where === home || (where !== 'CN' && !home)) return null;
  return {text: where, tone: where === 'CN' ? 'bad' : 'warn'};
}

function verdictText(verdict, home) {
  const note = UNLOCK_NOTES[verdict.note] ? `（${UNLOCK_NOTES[verdict.note]}）` : '';
  const where = verdict.state !== 'error' && verdict.region ? ` · 解锁地区 ${regionName(verdict.region)}（${verdict.region}）` : '';
  const differs = unlockMark(verdict, home) && home ? `，与 IP 所在地${regionName(home)}不同` : '';
  return `${UNLOCK_STATES[verdict.state] || '未知'}${note}${where}${differs}`;
}

export const ROUTE_CITIES = [['bj', '北京'], ['sh', '上海'], ['gd', '广东']];
export const ROUTE_CARRIERS = [['ct', '电信'], ['cu', '联通'], ['cm', '移动']];
const ROUTE_RANK = {gold: 3, silver: 2, plain: 1};
const ROUTE_ERRORS = {busy: '节点忙，没能开始追踪', node_error: '节点出错'};

// 三网回程: each carrier with its line in each city, and the worst line it shows anywhere for the
// card (a city whose route is hidden does not count). null before the first check.
export function routeView(node) {
  const checked = node?.route;
  if (!checked?.results) return null;
  const carriers = ROUTE_CARRIERS.map(([id, name]) => {
    const cities = ROUTE_CITIES.map(([city, cityName]) => {
      const r = checked.results[`${city}-${id}`] || {};
      return {key: `${city}-${id}`, city: cityName, carrier: name, line: r.line || '', tier: r.tier || '', rttMs: r.rttMs ?? null,
        reached: Boolean(r.reached), error: r.error ? ROUTE_ERRORS[r.error] || r.error : '', hops: r.hops || []};
    });
    const known = cities.filter(c => c.line);
    const worst = known.reduce((low, c) => !low || (ROUTE_RANK[c.tier] || 0) < (ROUTE_RANK[low.tier] || 0) ? c : low, null);
    return {id, name, cities, line: worst?.line || '', tier: worst?.tier || '',
      title: `${name}回程\n${cities.map(c => `${c.city}：${c.line || (c.error ? `失败（${c.error}）` : '未知')}${c.rttMs != null ? ` · ${Math.round(c.rttMs)} ms` : ''}`).join('\n')}`};
  });
  return {at: checked.at, ip: checked.ip, carriers};
}

// A route's hops as rows: a run of hops that did not answer is one row (from–to), and the run
// after the last one that did says the route goes quiet from there.
export function routeHopRows(hops = []) {
  const rows = [], last = hops.findLastIndex(h => h.ip);
  for (let i = 0; i < hops.length; i++) {
    if (hops[i].ip) {
      rows.push({...hops[i], key: String(hops[i].ttl), label: String(hops[i].ttl)});
      continue;
    }
    let j = i;
    while (j + 1 < hops.length && !hops[j + 1].ip) j++;
    const n = j - i + 1;
    rows.push({silent: true, key: `${hops[i].ttl}-${hops[j].ttl}`, label: n > 1 ? `${hops[i].ttl}–${hops[j].ttl}` : String(hops[i].ttl),
      note: n === 1 ? '' : i > last && last >= 0 ? `之后 ${n} 跳都没有响应` : `${n} 跳没有响应`});
    i = j;
  }
  return rows;
}

// The services' verdicts in SERVICE_ICONS order, coloured by the IPv4 check (the IPv6 one on a
// server with only that); each title has both families, though on a server with IPv4 a failed
// IPv6 check is left out: it mostly means the address has no way out. null before the first
// check. region: as for addressNetworks, what a service's country is compared with.
export function unlockServices(node, region = '') {
  const checked = node?.unlock;
  const family = checked?.v4 ? 'v4' : checked?.v6 ? 'v6' : '';
  if (!family) return null;
  const nets = Object.fromEntries(addressNetworks(node, region).map(e => [`v${e.family}`, e.country]));
  const home = f => nets[f] || (f === family ? node?.geo?.country || '' : '');
  const families = ['v4', 'v6'].filter(f => checked[f]);
  const items = SERVICE_ICONS.filter(icon => checked[family][icon.id]).map(icon => {
    const verdict = checked[family][icon.id];
    const lines = families.filter(f => checked[f][icon.id] && (f === family || checked[f][icon.id].state !== 'error'))
      .map(f => `IPv${f.slice(1)}：${verdictText(checked[f][icon.id], home(f))}`);
    return {...icon, state: verdict.state, region: verdict.state !== 'error' ? verdict.region : '', mark: unlockMark(verdict, home(family)),
      limit: verdict.state === 'limited' ? UNLOCK_NOTES[verdict.note] || '' : '', label: [icon.name, ...lines].join('\n')};
  });
  return {at: checked.at, family, items};
}

export function asnText(entry) {
  return [entry?.asn ? `AS${entry.asn}` : '', entry?.org || ''].filter(Boolean).join(' ');
}

export function googleRegionText(code) {
  return code === 'CN' ? '中国（送中）' : code ? `${regionName(code)}（${code}）` : '未知';
}

// Server names, groups and addresses are not identities or logins.
export const noAutofill = {'data-1p-ignore': '', 'data-lpignore': 'true', 'data-bwignore': '', 'data-form-type': 'other'};
export const INSTALL_STEPS = [['check', '连接并检查系统'], ['download', '下载节点程序'], ['enroll', '注册节点'], ['service', '启动服务'], ['online', '等待上线']];
export const EXEC_ERRORS = {node_offline: '节点不在线', node_not_enrolled: '未安装节点', capability_disabled: '节点关闭了命令执行',
  node_busy: '节点忙', node_connection_lost: '连接中断，结果未知', exec_failed: '执行失败'};
// Status codes whose generic wording is better than an unrecognised code.
const STATUS_FIRST = new Set([401, 403, 404, 413, 422, 429]);
export function safeError(error) {
  const code = error?.response?.data?.error;
  if (code && ERRORS[code]) return ERRORS[code];
  // Server routes return fixed codes or the node's own short error text, never
  // submitted values, so naming an unknown one beats a misleading guess.
  if (typeof code === 'string' && code && !hasErrorText(code) && !STATUS_FIRST.has(error?.response?.status)) return `操作没有完成（${code.slice(0, 80)}）。`;
  return instanceError(error);
}
export function errorText(code) { return safeError({response: {data: {error: code}}}); }

// Month-like cycles divide by whole months, so a monthly plan costs exactly its price.
const MONTHS = [[27, 32, 1], [87, 95, 3], [175, 185, 6], [360, 370, 12], [720, 750, 24], [1080, 1150, 36], [1800, 1850, 60]];
// One billing cycle before an expiry (YYYY-MM-DD), as the controller fills in a start date left empty.
export function cycleStart(expiresAt, cycle) {
  if (!expiresAt || !(cycle > 0)) return '';
  const [y, m, d] = expiresAt.split('-').map(Number), months = MONTHS.find(([lo, hi]) => cycle >= lo && cycle <= hi)?.[2];
  const pad = n => String(n).padStart(2, '0');
  if (!months) { const t = new Date(Date.UTC(y, m - 1, d) - cycle * 86400000); return `${t.getUTCFullYear()}-${pad(t.getUTCMonth() + 1)}-${pad(t.getUTCDate())}`; }
  const index = y * 12 + (m - 1) - months, year = Math.floor(index / 12), month = index % 12 + 1;
  const last = new Date(Date.UTC(year, month, 0)).getUTCDate();
  return `${year}-${pad(month)}-${pad(Math.min(d, last))}`;
}
// `months` after a date, or after today when there is none; a day the month lacks becomes its last (01-31 + 1: 02-28).
export function addMonths(date, months, now = new Date()) {
  const [y, m, d] = (date || zonedToday(now)).split('-').map(Number);
  const index = y * 12 + (m - 1) + months, year = Math.floor(index / 12), month = index % 12 + 1;
  const last = new Date(Date.UTC(year, month, 0)).getUTCDate();
  const pad = n => String(n).padStart(2, '0');
  return `${year}-${pad(month)}-${pad(Math.min(d, last))}`;
}
export function monthly(billing) {
  if (!(billing?.price > 0 && billing.cycle > 0)) return 0;
  const months = MONTHS.find(([lo, hi]) => billing.cycle >= lo && billing.cycle <= hi)?.[2];
  return months ? billing.price / months : billing.price * 30.4375 / billing.cycle;
}

// Currency as typed in a bill (symbol or ISO code) to its ISO code; '' when unknown.
const CURRENCY_CODES = {'¥': 'CNY', '￥': 'CNY', 'CN¥': 'CNY', 'RMB': 'CNY', '元': 'CNY', '$': 'USD', 'US$': 'USD', '€': 'EUR', '£': 'GBP', 'HK$': 'HKD',
  'NT$': 'TWD', 'S$': 'SGD', 'C$': 'CAD', 'A$': 'AUD', 'JP¥': 'JPY', '円': 'JPY', '₩': 'KRW', '₽': 'RUB', '₹': 'INR', '₫': 'VND', '฿': 'THB'};
export function currencyCode(value) {
  const text = String(value ?? '').trim();
  return CURRENCY_CODES[text] || (/^[A-Za-z]{3}$/.test(text) ? text.toUpperCase() : '');
}

// Monthly amounts per currency added up in CNY with fx.rates (CNY per unit);
// amounts without a rate stay apart in `rest`.
export function spendInCny(spend, fx) {
  const out = {total: 0, parts: [], rest: []};
  for (const [currency, value] of spend) {
    const code = currencyCode(currency), rate = fx?.rates?.[code];
    if (rate > 0) {
      out.total += value * rate;
      out.parts.push({currency, code, value, rate, cny: value * rate});
    } else out.rest.push([currency, value]);
  }
  out.parts.sort((a, b) => b.cny - a.cny);
  return out;
}

// Share of the current billing cycle already used, for the progress bar.
export function cycleProgress(billing, now = new Date()) {
  const days = billing?.cycle === -1 ? null : daysLeft(billing?.expiresAt, now);
  if (days === null || !(billing.cycle > 0)) return null;
  return Math.min(100, Math.max(0, (1 - days / billing.cycle) * 100));
}

// Midnight of a date in the servers' time zone (the browser's when unset or unknown), as a Date.
function zonedMidnight(date) {
  const utc = Date.parse(`${date}T00:00:00Z`);
  try {
    if (zone) {
      const format = new Intl.DateTimeFormat('en-US', {timeZone: zone, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric',
        hour: 'numeric', minute: 'numeric', second: 'numeric'});
      const offset = ms => {
        const p = Object.fromEntries(format.formatToParts(new Date(ms)).map(x => [x.type, Number(x.value)]));
        return Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) - ms;
      };
      // Twice: the offset at midnight itself, which differs from the first guess's across a clock change.
      return new Date(utc - offset(utc - offset(utc)));
    }
  } catch { /* an unknown zone: the browser's */ }
  return new Date(`${date}T00:00:00`);
}

// Linear projection of this period's traffic to the next reset day: the reset day of the next month, or its
// last day when shorter (as the controller's profile.period_start), in the servers' time zone.
export function projectedTraffic(traffic, now = new Date()) {
  if (!traffic?.periodStart) return null;
  const [y, m, d] = traffic.periodStart.split('-').map(Number);
  const next = new Date(Date.UTC(y, m, 1)), year = next.getUTCFullYear(), month = next.getUTCMonth() + 1;
  const last = new Date(Date.UTC(year, month, 0)).getUTCDate(), pad = n => String(n).padStart(2, '0');
  const start = zonedMidnight(traffic.periodStart), end = zonedMidnight(`${year}-${pad(month)}-${pad(Math.min(traffic.resetDay || d, last))}`);
  const elapsed = (now - start) / 86400000, total = (end - start) / 86400000;
  if (elapsed < 1) return null;
  return {bytes: traffic.used / elapsed * total, resetsOn: end};
}

// SVG path for a sparkline: values left to right, scaled into w×h.
export function sparkPath(values, w, h, top = 0) {
  if (!values?.length) return '';
  const peak = top || Math.max(1, ...values);
  const step = values.length > 1 ? w / (values.length - 1) : 0;
  return values.map((v, i) => `${i ? 'L' : 'M'}${(i * step).toFixed(1)},${(h - Math.min(v, peak) / peak * (h - 2) - 1).toFixed(1)}`).join('');
}

export function dateTime(ms) {
  return ms ? new Date(ms).toLocaleString('zh-CN', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false}) : '';
}
