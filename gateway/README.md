# OpenBear 独立认证网关

Go 1.24 标准库实现的独立服务；不启动主控或子实例，不包含另一套业务界面，也不自动部署、升级或重启任何已有服务。网关只内置独立最小登录页；登录后浏览器仍访问同一网关地址，由 Cookie 选定节点，页面、静态资源、业务 API 与 WS 全部来自该节点。各节点前端可以独立升级，不要求全家锁同版，也不是浏览器直连节点或新增 SSO。

正式使用时由主控「实例」页面安装和配置网关（见 `docs/instances.md`）；下面的构建、配置和协议说明面向开发与排查。

## 构建与运行

```sh
cd gateway
# 无第三方模块；可以禁用依赖网络。HOME/GOPATH 未设置的容器需指定隔离缓存。
export GOCACHE="${GOCACHE:-/tmp/openbear-gateway-build-cache}"
export GOPATH="${GOPATH:-/tmp/openbear-gateway-build-gopath}"
GOTOOLCHAIN=local GOPROXY=off go test -race -count=1 ./...
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o /tmp/openbear-gateway ./cmd/openbear-gateway
/tmp/openbear-gateway --version
/tmp/openbear-gateway --help
/tmp/openbear-gateway --config /path/to/gateway.json
```

配置示意（占位密钥必须替换为密码学随机值，例如 `openssl rand -hex 32`；配置文件应仅服务用户可读）：

```json
{
  "listen": "127.0.0.1:18962",
  "publicUrl": "http://127.0.0.1:18962",
  "stateDir": "/path/to/private/gateway-state",
  "controlKey": "REPLACE_WITH_A_UNIQUE_RANDOM_CONTROL_KEY"
}
```

- `listen` 默认 `127.0.0.1:18962`，提供登录入口和业务代理。可选 `controlListen`（必须与 `listen` 不同）单独提供控制接口；设置后 `listen` 上的 `/control/*` 和 `/api/gateway/probe` 一律 404。主控安装时，若能访问目标服务器的回环地址（本机，或节点在线且支持 `http.local`），会把 `controlListen` 设为 `127.0.0.1:<空闲端口>`，控制接口只在本机可访问；只有 SSH 的服务器不设置，控制接口与登录入口共用端口、以 controlKey 鉴权。主控端口不受接管。
- `publicUrl` 必须是无用户信息、query、fragment 的 HTTP(S) 根 origin，允许域名、公网 IP、IPv6、非标准端口。浏览器必须用这个 origin 访问；Origin/CSRF 校验不根据不可信转发头放宽。
- HTTP 可以直接正常登录、访问 API 和 WS；Cookie 的 `Secure` 只取决于配置的 `publicUrl` 是否为 HTTPS。不自动跳转 HTTPS，不要求任何指定网络隧道。
- 可选 `tlsCert` / `tlsKey` 必须成对配置，用于家庭监听的直接 TLS；也支持前置反向代理终止 TLS。独立控制监听为 HTTP。
- 不需要本地 `web/dist`。旧 JSON 的 `webRoot` 字段仅兼容读取，完全不打开或提供该目录，目录不存在也可启动。`stateDir` 仍为私有持久状态目录，不能通过业务节点另行公开。
- `gateway/login.html` 使用 `go:embed` 编译进二进制，发行源码构建须包含该文件。登录页无外部字体、脚本、CSS 或框架依赖；支持深浅主题、16px 手机输入、标准 form submit。
- SIGINT/SIGTERM 会取消业务流和 WS，并关闭所有监听。`--help` / `--version` 不读取配置、不绑定端口、不写状态。

## 控制协议 v1

`/control/v1/*` 位于 `controlListen`；未配置 `controlListen` 时位于主监听。必须携带 `Authorization: Bearer <controlKey>`，响应为 JSON / `no-store`，不回显密码或密钥：

- `GET /control/v1/status`：`{ok,version:1,build,publicUrl,clientIp,instances:[{id,revision,enabled,username}],outboxPending,outboxDropped}`。`version` 是状态文件格式号；`build` 是网关启动时计算的自身程序 sha256（读不到时为空），主控据此判断网关是否需要更新。
- `PUT /control/v1/instances/{id}`：`{revision,upstream,businessKey,username?,password?,enabled,reportUrl,reportToken}`。
  - 首次必须提供账号和密码；用户名区分大小写、全局唯一，1–128 UTF-8 字节且无首尾空白/控制字符；密码 1–1024 UTF-8 字节（长度和强度建议由主控界面提示，不在网关强制）。之后省略账号字段保持当前值；空串不是保持值。
  - revision 必须为正整数。相同 revision、相同配置幂等 ACK；同 revision 不同内容及旧 revision 返回 409。幂等比较不会将密码变成快速离线口令摘要。
  - 用户自己改名/改密不会修改配置 revision。主控旧请求重试可收到当前用户名 ACK，但不会恢复旧账号；主控应省略不打算显式覆盖的账号字段。
  - 路由、业务 key、账号、停用变化会持久撤销该实例全部会话并取消在途请求/WS/SSE。仅上报配置或 revision 变化不打断业务。
  - 三种凭证（controlKey、businessKey、reportToken）必须独立；每项 32–4096 字节。实例 ID 允许字母、数字、`_`、`-`，最长 128 字节。
  - `upstream` 必须为 HTTP(S) 根 origin；`reportUrl` 为完整 `/api/instance-events/v1` URL。禁止把已知主控上报 origin 或网关自身 origin 登记为上游。主控仍负责识别其 DNS 别名/其他入口，不得登记主控为子实例。
- `GET /control/v1/instances/{id}/probe`：使用该实例业务 key 和实例头实际访问 `/api/gateway/v1/probe`，返回 `{ok,id,revision,reachable,status,httpStatus?,error?}`。`status` 是下游实际身份/健康/持久化信息，不由网关伪造；错实例 `reachable:false`。节点报告 degraded 时保留其真实证据，由主控判定健康。

控制 API 只认 controlKey，Cookie 登录不能调用。另有精确诊断端点 `GET /api/gateway/probe`，同时要求 control Bearer 和 `X-OpenBear-Probe-Instance`，只返回同样的探针结果，不构成无 Cookie 业务代理。探针和登录事件上报均禁止跟随重定向；HTTPS 默认正常校验证书。

## 客户端 IP 策略

`Config.clientIp` 可提供初始策略；未配置时为 `{source:"auto",trustedProxies:[]}`。旧 state 缺字段也能启动。首次初始化会持久化实际策略；已有持久策略优先于启动配置，不被旧配置覆盖。

`PUT /control/v1/client-ip`（同 controlKey Bearer）接收完整对象：

```json
{"source":"header","trustedProxies":["203.0.113.10/32"]}
```

原子持久成功后热应用，ACK `{ok:true,clientIp:<规范化实际策略>,clientIpObserved:<按新策略重算 trusted 的最近观察>}`；GET status 回读实际值。失败不 ACK、不应用。策略变更只清理旧来源识别的限流桶，不撤销会话或更改账号/路由；同值 PUT 不重置限流。

- `source` 为 `auto`、`header` 或 `socket`。`auto` 只把本机回环地址视为可信代理，另外对 Cloudflare 公布的边缘网段只读取 `CF-Connecting-IP`（该头由 Cloudflare 覆盖）；内网中的其他代理不自动信任；`header` 在此基础上再加上 `trustedProxies` 中的地址；`socket` 只用连接地址、忽略所有转发头。可信列表最多 128 条 IP/CIDR；单 IP 转 /32 或 /128，CIDR 清除 host bits，去重、按字符串排序；拒绝 zone、非法地址/字段及 header 模式空列表。
- status 的 `clientIpObserved` `{peer,header,atMs,trusted}` 是最近一次带转发头的请求：可信代理（`trusted:true`），或头被忽略的内网对端（`trusted:false`，供主人在网关设置中一键确认）。旧版网关只报告可信代理且没有 `trusted` 字段；主控升级时把旧版报告过的内网代理写入 `trustedProxies` 一次，现有内网反代部署不受影响。
- 直接对端是可信代理时，按固定顺序读取 `CF-Connecting-IP`、`True-Client-IP`、`X-Real-IP`、`X-Forwarded-For`、`Forwarded`，取第一个有效值；缺失、无效或来源不可信均退回连接地址。旧策略中的 `header` 字段仅兼容读取，不再决定读取哪个头。
- XFF / RFC7239 Forwarded 从右向左剥除可信代理，使用首个非可信 IP，防左侧伪造；链最多 128 跳、8192 字节。单值头拒绝多行重复、逗号多值或非法地址。支持 IPv4/IPv6 和标准 Forwarded 的引号/端口格式。
- 限流与会话 IP 在每次认证尝试中使用同一策略快照。所有上述来源头均从发往节点的请求剥离，不作为节点鉴权凭证。
- 配置 Cloudflare/Nginx 等前置代理时，可信列表必须只包括实际代理来源，代理须覆盖或清理所选头；过宽信任范围或保留任意客户端单值头会允许伪造。HTTP 可用，不要求更改网络架构。

## 浏览器协议与防串号

1. `GET /api/auth/mode` → `{ok:true,mode:"gateway"}`。
2. `POST /api/auth/password/login`，JSON `{username,password}` → `{ok:true}` 和 `openbear_gateway_session` Cookie。已有有效会话时返回 `409 logout_required`，必须先退出；不静默换人。
3. 新页面只用 Cookie 调用 `GET /api/auth/session`，得到 `{ok,mode,chatId:1,instanceId,username,contextToken,expiresAt}`。已有页面若提供 context 则同样校验，不能重新 bootstrap 接管别人的 Cookie。
4. 每个业务 API、附件、下载、图片和 WS 同时提交 Cookie 与页面保存的 context：优先 `X-OpenBear-Context`，不能加 header 的请求使用 `_ob_ctx` query。无 Cookie 为 401；Cookie 有效但 context 缺失/错误为 `409 session_context_changed`。不重定向到别人的账号、不把旧操作自动重放。query context 在转发前删除，其他 query 字节/重复键/顺序保留。
5. `POST /api/auth/logout` 必须提供原 context，持久撤销自身会话并断流；仅清该 Cookie，不用 `Clear-Site-Data`。
6. `GET /api/auth/sessions` → `{ok,items:[{id,createdAt,expiresAt,lastSeenAt,ip,userAgent,current}],total}`；`DELETE /api/auth/sessions/{id}` → `{ok,sessionId,current}`。仅访问同一实例会话。时间字段为 Unix 秒，ID 为正整数。
7. `POST /api/auth/password/change` JSON **`{currentPassword,password,username?}`**，使用已有页面 context；成功 `{ok:true}` 后撤销该实例所有旧会话并清自身 Cookie。

`GET/HEAD /login` 始终由网关内置页面提供。已有 Cookie 时展示“继续当前登录”或“退出并切换账号”，不会静默换人。`next` 仅接受同源根相对、精确应用页面，保留通知 `_ob_instance`，拒绝外部/协议相对 URL、API、资源及登录循环；旧 `_ob_ctx` 不带入新登录导航。

节点 bootstrap 仅允许 GET/HEAD 精确应用页面 `/`、`/chat`、`/memory`、`/secrets`、`/docs`、`/mcp`、`/skills`、`/settings`、`/statistics`、`/webhooks`、`/cron`，以及 `/_i/<实例>/assets/...`、兼容旧节点的 `/assets/...`、`/openbear-push-sw.js`、`/manifest.webmanifest`、三个固定 `/icons/` PWA 图标。`/_i/<实例>/` 中的实例必须是当前会话的实例，否则 `409 session_context_changed`；转发给节点时去掉该前缀。必须有有效 Cookie，提供 context 时仍严格校验。匿名文档导航转到内置登录页；匿名资源返回 401，不选默认节点。API、业务文件和 WS 继续要求双校验；构建信息位于 `/api/system/version` 的 `frontend` 字段，**没有** `/build-info.json` 匿名兜底。

同一 origin 下的实例各自从 `/_i/<实例>/` 加载构建资源：网关在发往节点的页面请求上带 `X-OpenBear-Gateway-Assets: /_i/<实例>/`，支持前缀的节点据此在 index.html 中写入自己的前缀，编译后的 JS/CSS 按自身地址相对解析其余资源。节点静态响应保持 MIME。`/_i/<实例>/assets/` 内带内容哈希的构建文件使用浏览器私有长期缓存（`private, max-age=31536000, immutable`），保留 ETag/Last-Modified 和条件请求，允许 304 复用；节点升级后新哈希文件会正常重新获取。不支持前缀的旧节点仍用 `/assets/`，这些响应一律 `no-store`，避免缓存条目被同 origin 的其他实例使用。页面、固定名称资源、业务接口及错误响应仍为 `no-store`，其中 bootstrap 请求移除旧 If-None-Match/If-Modified-Since。

通知 Service Worker `/openbear-push-sw.js` 由网关内置提供（与 `web/public/openbear-push-sw.js` 相同，`no-store`），不转发给节点；其他任何带 `Service-Worker` 请求头的脚本请求一律 `403`，节点无法在该 origin 注册 worker。不新增离线业务缓存。

context 本身不能登录。会话最长 30 天、非滑动过期，到期也会断流。业务 Cookie / Authorization、客户端内部认证头及伪造 forwarding 头均不传子实例。网关重建 `X-OpenBear-Gateway-Key`、`X-OpenBear-Instance`、`X-OpenBear-Gateway-Session`；最后一项是独立随机值，不是原 Cookie。

代理保留方法、请求体、状态码、Range、内容编码及流式传输；不缓冲整份上传或下载，不给长业务流强加总时限。WS 通过标准库升级代理双向转发。响应移除子实例 Cookie/清站指令及内部认证头，业务响应 `no-store`。同上游重定向改为本网关路径；query 模式只向内部重定向续带 context，外部重定向不追加 context。业务代理不开放 `/api/managed/*`、`/api/instances*`、网关/认证控制命名空间、重启/升级接口。

## 节点推送会话核验

有效业务代理另外重建两个可信头：`X-OpenBear-Gateway-Origin` 为配置中校验、去根路径后的 `publicUrl` origin，`X-OpenBear-Gateway-Expires` 为当前会话过期时间（Unix 秒）。它们与既有随机 `X-OpenBear-Gateway-Session` 一起提供给已鉴权节点；浏览器伪造值先全部剥离。匿名 Webhook 不附加这些头。

节点在后台推送实际投递前可调用家庭监听的唯一精确只读端点：

```http
GET /api/gateway/v1/session-status
Authorization: Bearer <该实例businessKey>
X-OpenBear-Instance: <该实例id>
X-OpenBear-Gateway-Session-Hash: <sha256(UpstreamID UTF-8)的小写64位hex>
```

有效且归属匹配的会话响应 `{ok:true,active:true,expiresAt:<Unix秒>}`；不存在、已撤销、到期、实例停用或该实例下找不到指定 hash 时响应 `{ok:true,active:false,expiresAt:0}`。无效凭证/错实例凭证为 401，状态存储故障为 503，响应一律 `no-store`。controlKey、Cookie 或页面 context 不能替代业务 Bearer。仅精确 GET 生效，不开放其他 `/api/gateway/*` 业务代理，也不签发 Cookie/context、不刷新会话时间、不返回账号或业务数据。

接口读取现有持久会话状态，无新增数据库或推送凭证。有效会话重启保持有效；退出、改密、停用、会话撤销及到期后重启不会恢复。节点必须在每次投递前重新检查，不得以本地旧缓存替代；不可达/异常应 fail closed，重试由节点队列处理。

定向测试：`go test -race -count=1 -run '^TestPush' ./...`，覆盖实际代理头、不同会话隔离、凭证/实例范围、只读性、并发撤销、存储失败、撤销/过期跨重启及匿名回调不获得可信会话头。测试仅使用随机临时凭证与 loopback fixture，不向真实推送服务外发；真实 Python 推送接线由对应 owner/主控验收。

## 外部回调

唯一匿名入口为 `/i/{instanceId}/webhook/...`，去掉 `/i/{instanceId}` 后，只允许当前应用的：

- POST `/webhook/{endpointId}`
- GET/HEAD `/webhook/{endpointId}/events/{eventId}`（HEAD 对应现有 GET 路由的标准行为）
- POST `/webhook/attempts/{attemptId}/telemetry`

保留原 Authorization、签名头、请求体、query，不附加业务 key、不转发 Cookie；最终仍由子实例验证原 Webhook 凭证。停用实例不可接收新回调。不要将普通业务 base URL 改为 `/i` 前缀。

## 持久性、限额与上报

- 不依赖额外数据库。`stateDir/state.json` 保存路由、PBKDF2-HMAC-SHA256 密码哈希（600,000 次、128-bit 随机盐、256-bit 输出）、Cookie SHA-256、会话绑定值和登录 outbox；不保存明文密码或原 Cookie。
- 状态目录 0700，文件 0600；临时文件写入、文件 fsync、原子 rename、目录 fsync 后才 ACK。进程内事务锁 + Linux flock 防多进程并写。写失败会关闭业务流并 fail closed，需恢复存储后重启；不谎报修改成功。撤销删除持久会话记录，重启不会复活。
- 上限 1,000 实例、10,000 总会话、每实例 100 会话；登录时清过期记录并淘汰最旧会话，淘汰同样断流。活动会话 lastSeen 最多每分钟持久更新一次。
- 登录/改密按来源 IP 与账号分别计算**失败额度**：各最多累计 10 次失败，每 30 秒恢复 1 次；校验前仅检查额度，确认失败后才记账。没有所有用户共享的认证处罚额度；成功、已被限流拒绝或 KDF 忙碌的请求不扣失败额度。成功会清理该账号的失败记录，并仅移除它对当前 IP 的失败贡献，不清空同 IP 针对其他账号的失败历史。来源识别统一使用上述 clientIp 策略；代理未被识别为可信时，其后的用户仍可能共享失败额度。
- 保留最多 4 个并行 KDF 作为计算资源保护（忙碌返回 429，不是密码失败）；达到失败阈值前已获准的少量在途校验可能继续完成。限流表最多 8,192 桶，每桶最多 10 条有归属的失败记录。拒绝 IP 时不会创建新账号桶；容量不足时先清过期记录，再淘汰最旧失败记录，不将满表变成新用户全局拒绝。限流仅驻内存；有界淘汰不保证保留无限多攻击来源的历史。
- 登录成功/失败生成 `{eventId,instanceId,type,atMs,data:{}}`，独立 reportToken Bearer 上报；未知账号失败只写本地有界审计（最多 256 条），不虚构实例归属。已知/未知账号的失败响应一致，不回显账号存在性。
- 单个 outbox worker，10 秒请求时限，指数退避至 1 小时，稳定 eventId 跨重启重试；收端按 eventId 去重。主控停机不影响本地认证或转发。
- outbox 最多 1,024 条；溢出淘汰最旧项，最多 20 次尝试或 7 天后终止，并累计 `outboxDropped`。这是有界、至少一次重试，不承诺永久断网或溢出后的无损审计；ACK 后删除已交付项。

## 验证范围与安全建议

本次节点前端/IP 定向验证：`go test -race -count=1 -run 'Test(ClientIP|NodeFrontend|GatewayLoginPage|GatewayWithoutWebRoot|LegacyWebRoot|BoundariesOriginStatic)' .`，覆盖双节点独立 index/JS/构建信息、无 Cookie 拒绝、资源版本变化、旧 context 拒绝、登录 next、可信链/单值头/IPv6、IP 策略持久热应用、失败不 ACK 及并发读取。仅使用临时随机凭证与 loopback 节点 fixture；最终 Python 节点和 Chrome 验收由主控负责。

失败限流定向验证：`go test -race -count=1 -run 'Test(FailureLimiter|LoginFailureLimiter|PasswordChangeFailureLimiter|UniformLoginErrorsRateLimitsAndUnknownAudit)' .`。使用确定性限流时钟与模拟 loopback 来源请求，覆盖持续受限 IP 不饿死其他 IP、连续合法退出登录不消耗失败额度、成功不抹掉同 IP 其他账号历史、失败/改密仍限流、满表不封禁新用户及 KDF 并发上限；无需部署或真实等待限流窗口。

`go test -race ./...` 使用临时目录、临时随机凭证和 loopback fixture，覆盖真实 HTTP/WS、文件/压缩/Range/流式上传、SSE、在途请求取消、Cookie/context 串号拒绝且上游未收到请求、管理边界、原回调认证、真实身份 probe、默认 TLS 校验、唯一用户名、revision 幂等、会话范围/撤销、写失败闭锁、锁冲突、outbox 重启重试/有界、限流，以及**实际 CLI 子进程**重启后登录/会话/撤销持久化。可离线交叉编译：

```sh
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o /tmp/openbear-gateway-linux-amd64 ./cmd/openbear-gateway
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -o /tmp/openbear-gateway-linux-arm64 ./cmd/openbear-gateway
```

这些不是实际 Python 子实例、最终前端浏览器、真实家庭网络、线上服务的验收；相关集成另由主控执行。arm64 交叉编译不等于 arm64 主机执行验收。

安全建议：HTTP 在不可信链路会暴露密码/Cookie，建议按自己的网络条件使用 HTTPS；但网关不要求 HTTPS。配置了回环 `controlListen` 时控制接口不对外；未配置时控制接口与家庭入口共用端口，安全性依赖独立强随机的 controlKey，不要泄露该密钥，需要进一步收紧时可在前置代理上限制 `/control/` 路径的来源。前置代理的访问日志应避免记录 Cookie、Authorization、`_ob_ctx` 或含凭证的请求体。同源代理明确**信任各节点的前端代码与通知 worker**：它们拥有同一浏览器 origin 的权限，Cookie/context 防串号不等于隔离互相恶意的节点。对不同信任级别应用可考虑分离 origin，但本服务不强制域名、HTTPS 或改为直连/SSO。妥善保护和备份私有状态/配置，其中必须保存转发与上报密钥；不要上传公开仓库。不实现自研传输加密或 mTLS。
