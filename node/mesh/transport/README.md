# Mesh transport（A2兼容接口 + A3双平面多路径）

`Bind` 实现业务 plane=0；`ControlBind()` 返回 plane=1 的 `PlaneBind`，共用物理socket。路径准备、认证归属和提交由本包实现；独立控制WG/TUN以及健康、nonce/ready/commit/归属协议由health接入层实现。

## 接口与接入

- `New(Options) (*Bind, error)`：Options 包含 `ListenUDP/ListenTCP/ListenTLS`、`TLSDir`、`MTU`（0=1400）、`ResourceProfile`（空/member/relay）、`Logf`。
- `ParseEndpoint`：只接受 `udp://host:port`、`tcp://host:port`、`tls://host:port`，不接受 fragment/query 中藏指纹。返回端点的 `DstToString` 保留协议前缀。DNS在解析配置时解析，连接使用该解析结果；地址更新应重新配置端点。A3另接受已注册peer的`mesh-peer://`稳定逻辑端点。
- `SetPins(endpoint, pins)`：TLS 叶证书 DER SHA-256，允许大小写及冒号格式，最多两个。TLS peer 创建前调用。缺失/无效指纹拒绝连接；已有路径会关闭不再受信的连接。安全错误停等有效的信任变更，不自动降级或循环尝试相同错误指纹。该约定方法没有 error 返回值，配置层应预先校验；ParseEndpoint/Stats 仍会体现拒绝。
- `Reconfigure(Options) error`：先建立新监听，再提交并关闭旧监听及其接受的连接；绑定失败保留旧配置和会话。相同 TLS 端口可重新加载 Active 证书，已有 TLS 会话不因正常轮换被踢下线。资源缩减无法容纳当前连接时拒绝，不偷偷超配。
- `Release(endpoint string)`：删除主动端点后取消并等待拨号任务退出；被动端点则关闭对应接受连接。调用参数是配置端点或 WireGuard 当前端点的完整 URI。端点引用不可再用于 Send。删除最后一个出站 UDP 端点且未开公网 UDP 监听时，回收临时 UDP socket/接收协程。
- `Stats() []PathStats`：端点URI、传输、socket本地/远端地址、状态、主动/被动及认证确认、证书代次/指纹、拨号/重连尝试、帧载荷收发字节、丢弃、收队列字节、正在写的帧字节、实际socket缓存配置量、错误和观测时间。收字节是Bind收到的封装载荷，不冒充WireGuard认证后的业务字节；Reconnects为首次拨号之后的拨号尝试数，不是成功恢复数。
- `EnsureCertificate(dir) (Certificates,error)`：返回 Active 和可选 Pending。首次生成 Active；Active 距到期不超过30天时生成 Pending，但不自动启用。
- `ActivateCertificate(dir,id) error`：只能启用当前 Pending（或幂等确认 Active）；主控确认成员已获得新pin后调用，然后由A1调用Reconfigure加载。

A1必须在 `device.NewDevice` 之后、创建任何peer之前调用：

```go
dev.DisableSomeRoamingForBrokenMobileSemantics()
```

这是真正阻止迟到 UDP 把主动 TCP/TLS 端点覆盖的 Device 策略，不能由 Bind 代替。被动peer首次创建不填 endpoint；恢复缓存UDP地址时，再单独设置。显式端点与无端点之间切换策略需要重建该目标peer，不能假设已有peer的漫游开关会随UAPI自动改变。无关peer不重建。

`Open`使用Options中的监听端口；非零Open端口与非零ListenUDP不一致时拒绝。ListenUDP=0不开公共UDP监听；只有配置了出站UDP端点才建立系统分配的临时UDP socket以收回包。返回实际已有UDP端口（纯TCP/TLS时为0）。在线UDP换端口经Reconfigure执行，因此设备旧IpcGet listen_port可能未同步，实际地址以Stats/Options为准。

接收侧 `BatchSize=1`；Send仍接受任意多包批次（WG发送批量可能由TUN提高到128），按传入顺序逐包尝试发送。单包出错继续处理后续包，最终返回首个错误；空批次为成功空操作。不会重放半帧或把整批复制进发送队列。不实现socket mark和出口网卡绑定；非零SetMark返回明确错误。UDP LocalAddr是socket绑定地址，可能为通配地址，**不是公网地址或已确认的实际出口接口**；不能据此在UI显示“已绑定出口”。TCP/TLS LocalAddr是连接的实际本地端点。未配置出口约束时采用系统路由，不修改既有VPN/代理策略。

## 帧、连接与资源

- stream双方各发送一次 `OBM2`，随后每帧 `uint16_be length | uint8 plane | WG datagram`，length含plane。UDP为 `OBM2 | plane | WG datagram`。
- 接受plane=0/1；控制密文额外限制`maxDatagram(1044)`（IPv4头20+控制载荷1024）。异常版本/长度/plane、截断帧不尝试恢复。stream断开，UDP丢弃。最大WG载荷为 `max(148,ceil(MTU/16)*16+32)`；MTU支持576～65488，通常用1280/1400。
- 拨号和TLS握手各5秒；应用前导5秒。连断即关闭，后台指数退避0.5秒起、最多30秒，±20%抖动且封顶30秒；稳定10秒后重置。无可写连接直接返回错误，不缓存待重连业务。
- TLS只允许1.3，pin与证书有效期均检查；关闭session tickets。证书P-256、自签90天，提前30天只准备下一代。不存在不验pin的“首次信任”。
- TCP/TLS每连接一个写入者，业务和控制各最多4个独立等待名额，平面内FIFO；每帧结束后优先移交给控制等待者，不再因控制竞争直接丢业务包。等待者只持有通知状态，不复制数据或创建发送goroutine；调用方在Send返回前持有其原批次。每次Send整批共用5秒等待+写入期限，不按报文数累加。名额耗尽或等待超时返回ErrBackpressure，连接关闭立即唤醒等待者；半帧写失败先关闭连接，再唤醒下一等待者，不重放。TxBufferedBytes只统计正在写的帧，不包含WireGuard自身队列/调用方保留的批次。
- UDP路径不设单一写入者：同一路径上业务、控制两个平面各最多5个报文同时在socket调用中（与stream的1个写入者+4个等待名额相同），超出才返回ErrBackpressure并计Dropped，不等待、不排队。以前同一路径同一时刻只允许一个报文，业务批次发送期间写入的探测、握手响应都被直接丢弃（一个持续发送的业务方旁边，控制报文400个丢397个）。TxBufferedBytes是正在写的报文字节之和。
- TCP/TLS不显式设置SO_RCVBUF/SO_SNDBUF，保留内核自动调节；Linux仍设置TCP_NOTSENT_LOWAT=16 KiB。UDP没有TCP式自动调节，仍申请256 KiB收/发缓存。建立socket时读取实际SO_RCVBUF/SO_SNDBUF用于接入预算和状态初值（Linux可能倍增）。TCP后续自动增长不被该初值追踪，SocketBufferBytes不是实时内核占用或硬内存上限；操作系统额外结构开销也不包含在内。其他平台不声称具备Linux的NOTSENT_LOWAT能力。
- member/relay总stream连接上限16/128、未认证8/16、同一来源地址未认证最多4。socket建立时缓存大小合计接入预算16/64 MiB，可能先于连接数上限达到；该预算不阻止已建立TCP连接后续自动增长。
- 业务接收队列最多256个报文，控制另有64个报文槽；共享8/16 MiB全局字节上限及256 KiB单路径上限，全局保留64 KiB和单路径32 KiB给控制；正在解析的每个stream最多一个MTU大小的帧。UDP有一个固定约64 KiB读取缓冲，不按来源无限分配缓存。满后丢弃并计数，不增加等待队列或goroutine。

### 两个WG实例的固定预算

Bind与ControlBind实现可选`conn.ResourceBound`，只对mesh设备启用固定池、BatchSize=1和深度4的设备/peer队列。原生TUN的GSO拆包仍由meshTUN读取泵处理；不得把此小批次模式用于要求完整批次的裸原生TUN。业务和控制各自保留TX/RX/keepalive槽，发送信用耗尽时阻止下一次读取调度器，不借用控制额度或分配新消息数组。

- member：业务32 TX＋64 RX＋4 keepalive，控制8＋16＋4，共128个槽，按每槽64 KiB为**8 MiB**。
- relay：业务64＋128＋4，控制16＋32＋4，共248个槽，为**15.5 MiB**。
- 上限覆盖所有peer，注册到现有16/128个peer上限也不扩大池。元素/容器同样固定预分配；计算方式及生命周期见`node/third_party/wireguard-go/OPENBEAR-PATCHES.md`。TX耗尽可被Close取消，RX耗尽复用读缓冲并丢入站包，keepalive有独立预留，避免满池锁死。
- 以上为WG消息存储硬界限，不是整个进程RSS硬限制；调度器、transport、peer密码状态、栈和运行时仍单独占用。普通非mesh WG设备不采用这些限制。

### 认证确认

使用锁定版本的最小WG适配（见`node/third_party/wireguard-go/OPENBEAR-PATCHES.md`）。真实握手认证或type4 AEAD+反重放通过后，Bind才确认认证；cookie不算。没有收到认证包的stream仍有10秒期限，合法单向收包不要求“必须回包”才能续存。发送type2/type4本身不再冒充入站认证。

A3注册peer后，两个平面的key必须映射到同一nodeID；未知key、跨peer路径抢占和旧代次拒绝。认证并不自动提交业务路径，也不替代上层nonce挑战/配置代次/路由授权。

证书定期检查由调用EnsureCertificate的一方调度；Bind的New/Open/Reconfigure也检查并准备Pending，但没有独立后台自动启用计时器。整个证书目录用跨进程锁串行操作；代次PEM和状态文件分开，状态原子替换。目录0700、文件0600；Windows设置仅SYSTEM和Administrators的受保护ACL。历史代次文件保留，不自动删除仍可能被部署引用的证书。

## 验证

本包测试使用内存TUN、真实WireGuard和本机socket，没有修改主机网卡、路由、防火墙或qdisc，也没有远程操作。

```sh
cd node
export HOME=/root GOTOOLCHAIN=local GOCACHE=/tmp/go-build
go test -race -timeout=240s ./mesh/transport/...
GOOS=linux GOARCH=amd64 go vet ./mesh/transport/...
GOOS=windows GOARCH=amd64 go vet ./mesh/transport/...
GOOS=darwin GOARCH=arm64 go vet ./mesh/transport/...
```

覆盖：拆/粘帧、截断、超长、错误plane；UDP/TCP/TLS双向WG；断线恢复；真实迟到UDP密文正常交付但不改变固定stream；NAT端口变化；TLS错误pin停等、过期拒绝、TLS1.2拒绝、双pin轮换；监听绑定失败回滚、关闭/Release回收、旧ReceiveFunc关闭语义；cookie不被误当认证、未认证额度/期限；慢接收者背压及无持续缓存增长；Linux实际NOTSENT_LOWAT及保持原内核缓存配置；双平面三传输128包批量顺序、中间坏包后继续发送。

A3回归与结果见文末。Windows/macOS当前是交叉vet，不是真机。全网故障检测/归属传播、附录规定规模的长稳及全部WG池内存预算仍由集成验收覆盖。

## A3 API（2026-10-26）

接口已实现，可从`paths.go`、`control.go`及WG的`conn/openbear.go`读取完整定义。主会话已批准锁定版本 WG 的最小适配；不改加密、线格式、反重放。health负责nonce/mesh/configGeneration/prepare-ready-commit/归属协议；transport不因认证包到达自动提交路径。

公开接口：

```go
type PeerIdentity struct { ID string; DataKey, ControlKey [32]byte }
type PathRef struct { ID string; Generation uint64 }
func (*Bind) RegisterPeer(PeerIdentity) error
func (*Bind) RemovePeer(peerID string) error
func (*Bind) PeerEndpoint(peerID string) (string, error) // mesh-peer://<id>，供两台WG设备UAPI使用
func (*Bind) ControlBind() *PlaneBind // plane=1，共享物理I/O，不另开监听
func (*Bind) Prepare(ctx context.Context, peerID, endpoint string) (PathRef, error)
func (*Bind) PathEndpoint(PathRef) (conn.Endpoint, error)
func (*Bind) Commit(peerID string, path PathRef) error
func (*Bind) Deactivate(peerID string, expected PathRef) error
func (*Bind) ReleasePath(PathRef) error
func (*Bind) PathInfo(conn.Endpoint) (PathInfo, bool)
func (*Bind) Events() <-chan PathEvent
func (*Bind) Paths() []PathInfo
func (*Bind) Resources() ResourceStats
```

`Prepare`只等待socket和OBM2就绪，不把TCP connect视为路径ready；TLS仍先SetPins。重复URI复用原路径，代次随重连改变；健康结果必须绑定PathRef。`Commit`只允许绑定到同peer且认证通过的当前代次；只原子选择本地业务发送路径，不冒充远端提交或全网路由原子性。ReleasePath拒绝释放仍活动的路径；已经关闭并移除的引用可幂等释放，旧代次不能误删新连接。临时释放保留TLS配置pin，撤销用RemovePeer删除peer与对应信任。首次Prepare失败/取消会回收新建拨号任务，重复Prepare已就绪的路径失败不会销毁它。每peer最多活动、热备、临时共3条。

跨中转切换时，在业务AllowedIPs已切离旧peer后调用`Deactivate(oldPeer, expected)`，然后由health在最多2秒收尾后ReleasePath，或继续把连接作为热备。Deactivate用完整ID+generation比较当前active，成功仅清active及等于expected的bootstrap，不删身份、不关连接、不碰其他peer/路径，发`deactivated`事件；连接已断开仍可清除其残留选择。不匹配（含重复调用/空expected）返回`ErrPathChanged`且不改状态；未知peer返回`net.ErrClosed`。旧事件不得通过读取“最新active”后无条件重试来绕过CAS。同中转换路径仍只需Commit。Deactivate不取消已取得旧socket的在途写入，业务路由切换与收尾由health保证。

新WG可选接口定义于`node/third_party/wireguard-go/conn/openbear.go`：`ReceiveMetadata{PublicKey,Endpoint,MessageType}`；Bind的`AuthenticatedReceive`只在认证成功后调用（type4必须过反重放），可拒绝身份错配；TUN的`WriteWithMetadata`收到逐包相同metadata。`SendMetadata{Endpoint,ID}`从TUN的`ReadWithMetadata`逐包穿过加密，Bind的`SendWithMetadata`获得真实目标peer公钥。健康接入层据此提供`SendControl(PathRef,payload)`，不发送明文plane=1，不临时改peer.endpoint。握手可显式指定路径，响应沿请求路径返回。

PathInfo包含peer、PathRef、端点、plane、收包时间，以及该plane最近认证时间和累计认证包数；PathEvent包含连接/断开/认证及发送结果，发送结果含SendID、WriteStartedAt、WriteCompletedAt、Err。socket写完成不等于网卡上线，不能称为排除了内核排队。事件通道有界，不代替权威快照，溢出计数必须可见。内部认证状态在锁内更新，只投递有界事件，不调用任意上层函数。Events容量256，丢弃计数在Resources.DroppedEvents；消费者必须用Paths/Stats重建权威状态，不能把事件完整性作为正确性前提。

数据Bind先Open，ControlBind随后Open；关闭控制Bind只停plane1，不关业务；关闭数据Bind关整个物理代次，两者旧ReceiveFunc均退出。两个Device共享socket/传输预算，独立WG池仍须计入进程整体160/256 MiB验收；本补丁范围不静默修改上游池/worker算法。

### 控制WG接入顺序

1. 创建设备时仍在添加peer前调用`DisableSomeRoamingForBrokenMobileSemantics`。注册所有PeerIdentity，业务与控制UAPI都用PeerEndpoint返回的逻辑端点。
2. 先启动业务Device（打开物理I/O），后启动控制Device。Prepare候选得到PathRef；peer初始化握手可显式调用`SendHandshakeInitiationTo(false, PathEndpoint(ref))`。目标在5秒节流判断前记录，握手未完成时原定时器沿该精确Endpoint重试，首包丢失/本地背压后不需要通过反复Warm抢定时器。完成、重试耗尽、停止或密钥清理时清空记录。重连后的旧generation不会自动升级，上层需提供新PathEndpoint。已有活动业务会话的同peer候选仍只用`SendKeepaliveTo`，不要用重新发起握手来探测；记录本身不改变active，普通业务也不能使用未提交的握手目标。
3. 控制TUN通过ReadWithMetadata提供IPv4包和`conn.SendMetadata{Endpoint:PathEndpoint(ref), ID:sendID}`；WriteWithMetadata收到后调用PathInfo(metadata.Endpoint)，并核对metadata.PublicKey。控制payload最大1024字节、源/目的控制地址和协议白名单由控制TUN强制校验；绝不连接系统TUN或普通业务转发。
4. 校验nonce、mesh/revision和实际PathRef后完成ready/commit协议，再调用Commit。不同入口首次成为候选时业务WG也应显式预热握手，不能把控制会话ready当成业务WG会话ready。可调用`peer.SendKeepaliveTo(endpoint)`发送指定路径的空加密确认包（无返回值、有界队列，调用不等于送达）。适配WG在type2派生会话之后也会自动向原入站Endpoint发空确认，不需提前Commit。type2认证回调早于会话派生，不能在回调到达时就宣称双端会话已经可发送；被动端需确认keypair真正完成，例如检查其WG握手完成状态。
5. `sent`事件仅在到达Bind时发出；若WG在加密前丢包，不会有写出完成事件，health必须按自己的截止时间判为本机未写出/失败。WriteStartedAt为取得写入槽之后，WriteCompletedAt为socket写返回；未到写入即拒绝时两者为零，不更新网络RTT。
6. 控制与业务共享同一TCP字节流，控制优先只能改善尚未写入的排队，不能穿越已经写出的丢失字节或替代路径限速。上层限制控制流量频率，发送等待者本身有硬上限。

实际字段与WG钩子签名已通过后续协调请求回复给健康工程师；接口定义以本包与`conn/openbear.go`为准。

### 出口地址探测、可达性测试与打洞（`reflect.go`、`stun.go`、`ttl_*.go`）

- 探测类报文都是32字节，以`OBR1`开头，第5字节是类型：1请求、2回包、3入站测试、4开路包。和带5字节帧头的WG报文不会混淆；`readUDP`在解帧前分流，不进WG，长度不对的一律丢弃。中转（`Options.Reflector`）只回应恰好32字节的请求，回包等长，不能被用来放大；按来源IP每秒8个、总计每秒1024个限速，超出的丢弃。
- `Reflect(ctx, server, id)`从WG使用的同一个UDP socket发请求，只接受来自该server、id匹配的回包，所以得到的就是业务报文的NAT映射。同时等待的请求最多16个。中转把回应过的请求按id记住10秒（最多1024个），`ReflectSeen(id)`给出它当时的来源地址；来源IP另记5分钟（最多4096个），`RecentlyFrom(ip, within)`据此及该IP的UDP路径（本机拨出的或已认证的）判断对方NAT上可能已有到本机的会话。
- `STUN(ctx, server)`从同一socket发RFC 5389 binding请求，只接受该server对本机事务的响应，解析XOR-MAPPED-ADDRESS（或MAPPED-ADDRESS）。
- 入站测试：`SendTest(target, id, sender)`从一个临时socket（与监听socket无关，因此不匹配目标在NAT上为本机监听端口开的会话）向target发3个测试包，间隔300ms；同时最多32个在发。被测节点`WatchTests(id)`后，收到的测试包按发送方（`TestRelay`：被请求的中转换端口发；`TestOther`：另一台中转）在`TestsSeen(id)`里置位，`StopTests`结束；同时最多16个。测试目标只能是`ReflectSeen`给出的地址，由健康模块保证。
- `Prime(addrs, ttl)`向每个地址发一个开路包，IP TTL为ttl：Linux用每个报文的`IP_TTL`控制消息，不影响同一socket上的WG报文；其他系统没有逐报文TTL，在锁内临时改socket的TTL再改回，这一瞬间写出的WG报文也会带低TTL丢失，按普通丢包处理。收到开路包的一端直接丢弃。
- `PunchEndpoint(peer, addrs, until)`给`SendHandshakeInitiationTo`用：只允许业务平面、发给已注册peer的148字节type1握手，在until之前同时发往全部1～8个地址；过期后WG的定时重发被静默吞掉，所以一次尝试结束后不会再向对方公网地址发任何包。`PunchExtend(ep, addrs)`把地址加进同一个端点并立刻把最近一次握手发往新地址（另一端收到"已开路"时用）。响应从哪个地址回来，就由现有UDP入站逻辑生成哪条路径，之后按普通PathRef准备、提交和释放。
- `Options.UDP`：没有`ListenUDP`也保留一个临时端口的UDP socket（可达性检测和打洞要用），`UDPPort()`给出它的本地端口。

## A3本轮验证结果

最终源码在`HOME=/root GOTOOLCHAIN=local GOFLAGS=-mod=readonly GOCACHE=/tmp/go-build`下：

- `go test -race -timeout=240s ./...`：node、browser、mesh、health、transport全部通过；transport最终121.666秒。
- 上游`device`、`conn`、`replay`的`-race`测试通过，原接口调用方式仍可用。
- Linux/amd64、Windows/amd64、macOS/arm64：`go vet ./... ./cmd/...`通过；主机`go build ./...`通过。
- 对比锁定上游，除`OPENBEAR-PATCHES.md`外只新增`conn/openbear.go`、`device/openbear.go`，只修改`device/receive.go`、`device/send.go`；主模块`go.sum`字节未变。
- 100次失败UDP回探：同一活动TCP连接保留，本机业务p95曾测5.322417 ms。另有100次失败TCP Prepare验证没有残留拨号任务。
- 20次已就绪路径提交并恢复双向数据：本机p95为427.217 µs。**未包含health故障检测、提交协议、跨中转归属传播，不能当全网8秒验收。**
- 真实控制密文延迟期间，8300份业务包推进业务重放计数后仍可接收；重复/篡改控制密文不进入认证回调或明文TUN。
- 新物理连接上合法单向收包、没有向该连接回包，超过10秒仍保持；旧连接generation不能提交或冒用。
- 并发8个Prepare最多留下3个同peer物理路径；旧ControlBind句柄不能关闭新一代控制接收器。

### 双实例内存（Linux，非race）

使用`TestTwoPlaneMemoryBudget`，同一测试进程容纳两个节点、共四个WG Device（每节点业务+控制各一），TCP活动/TLS备用。每档发送12000份1400字节业务，64份一组短暂让步，实际负载约0.35秒；控制探测在负载后可用。socket缓存另列，不计入Go堆。

| 档位 | 本轮负载收到 | Go heap | 进程RSS | 进程峰值RSS |
|---|---:|---:|---:|---:|
| member | 11975 / 12000 | 6,691,672 B | 25,584 KiB | 30,704 KiB |
| relay | 11976 / 12000 | 6,754,712 B | 31,600 KiB | 51,020 KiB |

两个物理路径的socket缓存配置合计：主动节点1,703,936 B、入口节点2,555,904 B。短突发中因背压丢弃的业务没有伪装成零丢包。该回归同时计入控制实例并低于160/256 MiB，但它是小规模短突发，不是附录固定网格规模/长稳的最终内存验收，也不是进程RSS硬限制。该表是启用上述固定WG预算之前的历史短测；现实现已经按授权固定mesh池、队列及worker，不能再用该历史数值描述当前内存配置。

## 批量发送、TCP自动调节与发送仲裁验收

`udp.go`按序处理完整批次，单包失败不阻止后续有效包；TCP/TLS保留内核自动调节和16 KiB NOTSENT_LOWAT。`sender.go`提供有界优先等待，`stream.go`维护连接状态与回收；没有修改调度器、health或WG密码实现。

- `batch_test.go`覆盖三传输、双平面的128包顺序、中间坏包后继续、空批次。
- `sender_test.go`覆盖业务名额满后控制仍可入队、控制优先且平面内FIFO、等待超时撤销、Close唤醒、128包共用5秒总期限和半帧失败关闭。
- 两轮仲裁定向race通过（11.883秒）；transport单独完整race通过（122.312秒）；Linux/amd64、Windows/amd64、macOS/arm64全node模块vet与`go build ./...`通过。
- 显式握手重试路径补齐后的标准`build_node.py`复验：transport全套race通过（142.138秒），health包通过；mesh中的`TestHealthRealDataAndRelayRecovery`在初始双向业务通过后，等待故障切换状态超时（health_integration_test.go:75）。标准脚本停在测试阶段，未完成四平台产物构建，不能宣称全模块通过。先前transport首次业务偶发超时本轮未再现，未通过放宽断言处理；`bind_test.go`保留不含密钥的失败诊断。

负载环境为50 Mbit/s、60 ms RTT、配置上传限速45 Mbit/s。原`TestLoad`连续两次在iperf3启动之前失败：节点Ready但入口仍Active=false且Transport为空；该测试只等待进程Ready/配置代次。为取得负载数据，临时Go overlay只在原断言前补充等待入口Active且Transport=tcp，不改任何流量、时间或判定线，不写共享测试源码。副本及overlay位于`workspace/experiments/meshlab/a3-send-arbitration/`。私有mount namespace隔离`/run/netns`名称目录，仅使用本次oblab-lm/lr，避免与A1的同名实验相互清理。

补充就绪等待后的TestLoad通过（58.46秒），当前共享工作树包含A1同时进行的调度器调整，不能把性能差异全部归因于本包：

| 组别 | 吞吐 Mbit/s | ping p95 ms | p95增量 ms | ping丢失 | 内层TCP重传 | 调度队列丢弃 | 成员/入口峰值RSS MiB |
|---|---:|---:|---:|---:|---:|---:|---:|
| 空载 | — | 61.1 | — | 0/50 | — | — | 18.5 / 16.3 |
| 限速45 | 40.8 | 61.0 | -0.1 | 0/80 | 5 | 5 | 37.4 / 39.0 |
| 不限速 | 43.2 | 602.0 | 540.9 | 0/80 | 0 | 0 | 154.1 / 60.0 |

限速组满足吞吐≥35 Mbit/s、p95增量≤200 ms及成员/入口RSS≤160/256 MiB；进程统计包含独立控制WG。不限速组延迟不满足该限速验收指标，不能据此宣称任意负载都低延迟；RSS也不是内核socket内存的统计。原测试的Ready/实际Commit等待差异需要集成测试负责人处理，不能将overlay通过写成未修改原测试通过。隔离实验命名空间及临时mount目录均已清理。
