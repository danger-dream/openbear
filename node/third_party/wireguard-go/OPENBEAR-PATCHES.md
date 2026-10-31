# OpenBear 的最小路径元数据适配

## 来源与维护边界

- Module: `golang.zx2c4.com/wireguard`。
- 上游：https://git.zx2c4.com/wireguard-go
- 锁定版本：`v0.0.0-20261024220952-b3bc704ef0ee`。
- 完整 commit：`b3bc704ef0ee21a36298601a9e1e5ff90377ac52`。
- 从原 `node/go.sum` 对应的 Go module cache 完整复制，保留 `go.mod`、`go.sum`、LICENSE 和其他源文件。没有升级或引入依赖。主模块仅增加 `replace golang.zx2c4.com/wireguard => ./third_party/wireguard-go`。
- 不修改密码算法、消息线格式、nonce分配、AEAD验证、握手时间戳验证或重放窗口。AllowedIPs源验证保留原位置和原结果，只在其拒绝时多问一次可选的 `SourceAuthorizer`（见下）。钩子提供进程内元数据，不把对端自报字段当认证信息。

## 文件与钩子

### `conn/openbear.go`（新增）

定义可选接口，原 `conn.Bind` 和 `tun.Device` 不变：

- `ReceiveMetadata{PublicKey [32]byte, Endpoint conn.Endpoint, MessageType uint32}`：实际认证peer公钥和ReceiveFunc交回的原Endpoint。
- `AuthenticatedReceiver.AuthenticatedReceive(ReceiveMetadata) bool`：Bind实现后接收认证通知。返回false可拒绝该包继续进入本适配的数据面；不能让未经验证的包通过。
- `SourceAuthorizer.AuthorizeSource(publicKey, src) bool`：TUN实现后，AllowedIPs把源地址映射到别的peer时再询问一次，只能额外放行、不能拒绝AllowedIPs已放行的包。mesh中转各自按实测选路，去程与回程的下一跳可以不同，相邻中转转发来的包源地址因此不一定路由回它；数据面只对中转之间的peer放行网格地址和已宣告的网关网段，成员、入口和直连peer仍只按AllowedIPs。
- `MetadataWriter.WriteWithMetadata(bufs, offset, []ReceiveMetadata)`：TUN实现后，解密明文附带逐包peer/path，只有已通过源检查（AllowedIPs或SourceAuthorizer）的非空IP报文会进入此接口。
- `SendMetadata{Endpoint, ID}` 与 `MetadataReader.ReadWithMetadata(bufs,sizes,offset,[]SendMetadata)`：TUN给单包附加本地目标路径和关联ID，随异步加密保留。
- `MetadataSender.SendWithMetadata(bufs, publicKey, SendMetadata)`：Bind收到指定路径、真正由AllowedIPs选中的peer公钥及关联ID，可验证路径归属并记录写入完成。ID只在本机使用。

### `device/openbear.go`（新增）

- 向可选Bind通知认证结果。
- 单包显式路径发送保留设备关闭检查、net锁和tx统计。没有显式Endpoint时仍调用原SendBuffers；Bind未实现MetadataSender时可直接用原Bind.Send指定Endpoint。

### `device/receive.go`

- 握手init/response在原 `ConsumeMessageInitiation/Response` 成功后通知；握手自己的认证及时间戳防重放语义不变。
- type4在AEAD成功且 `replayFilter.ValidateCounter` 成功后通知，覆盖空keepalive和单向流量。无效/重复密文不触发通知。
- type4认证通知只证明peer身份及包的新鲜性，**不证明IP源地址获得业务授权**；源验证仍在原位置，AllowedIPs拒绝时由 `device/openbear.go` 的 `sourceAllowed` 询问TUN实现的SourceAuthorizer，只有通过者交给TUN。
- 启用适配的Bind，其握手响应沿本次请求Endpoint发送；收到type2并成功派生keypair之后，空加密确认包也沿该响应的原Endpoint发送，不依赖尚未Commit的业务逻辑端点。未启用适配仍用原响应/keepalive路径行为。
- 解密后WriteWithMetadata与bufs一一对应；不通过最后一个漫游端点或缓冲指针推断路径。

### `device/send.go`

- `QueueOutboundElement` 保存单包SendMetadata，回池时清空，避免复用污染/引用保留。
- TUN实现MetadataReader时逐包读取；每次读取前清空metadata数组。
- 有显式路径的批次按原顺序逐包发送；普通批次仍按原SendBuffers方式批量发送。密码计算/nonce及队列顺序不变。
- 新增 `Peer.SendHandshakeInitiationTo(isRetry, endpoint)`；原方法调用它并传nil。非nil Endpoint在本次实际发送时记录；落入节流期时，只有仍有握手在进行（重传计时器挂起）才记录，即不能让定时器反复抢先向尚未Commit的逻辑端点发送；没有进行中的握手时不记录，否则它会成为几分钟后下一次握手的目标，而那时路径可能早已释放、打洞窗口早已结束。当前握手的默认/定时重试沿该原始Endpoint（含精确generation）发送；向记录的目标发送失败时清除记录，同一条initiation改由peer自身endpoint发送。显式调用的失败照常返回调用方，不改道。显式握手仍遵守原5秒节流，不修改lastSentHandshake的节流时长，不为探测关闭重放。
- 后续非nil显式调用可更新待重试目标，但不修改peer.endpoint、transport active/bootstrap或已有keypair；SendKeepaliveTo/业务数据仍只使用各自的逐包路径/已提交路径，不读取握手重试目标。
- 原 `SendHandshakeResponse()` 保留，内部辅助方法接受可选入站Endpoint。
- 新增 `Peer.SendKeepaliveTo(endpoint conn.Endpoint)`（无返回值）：通过原staged/nonce/加密队列发送一份空type4报文，保留单包Endpoint，不改active或重置会话。显式调用不因已有staged包而跳过，但队列满仍按原有界规则丢弃；不能把调用返回当作已送达。原SendKeepalive传nil，仍保持队列非空时不额外入队的行为。

### `device/peer.go`、`device/timers.go`

- Peer增加一个由handshake.mutex保护的`handshakeEndpoint`引用；不是密码状态，也不是业务默认路径。
- 握手完成、重试次数耗尽、timer停止、ZeroAndFlushAll及ExpireCurrentKeypairs清空引用，不让完成/停用后的默认握手继续使用旧试探路径。
- 原定时器重试入口不变，通过SendHandshakeInitiation自然读取保存的目标。路径释放/重连不会悄悄升级Endpoint的generation；旧引用仍由transport拒绝，上层须用新PathEndpoint显式Warm。

### `device/resources.go`、`device/pools.go`、`device/channels.go`、`device/device.go`

仅实现`conn.ResourceBound`的mesh Bind启用固定资源模式，普通WG的池、批次、队列和worker默认值保留。`DeviceLimits`的缓冲、队列和worker在NewDevice时冻结；`Device.SetPeerLimit`只在线调整peer准入数，不重新分配这些固定资源。

- 两个plane分配独立定额，合计构成一个节点预算。每个设备的TX、RX、keepalive消息数组分别一次性分配，归还只回到原池，不能向另一池借用，也没有溢出分配。额度覆盖全体peer，不为新peer增加消息数组。
- mesh每次TUN读取1包、每条设备/peer队列深度4；WG加密/发送背压在少量包后阻止下一次meshTUN.Read，让上游调度器承担排队及CoDel。meshTUN自己的OS读取泵保留原GSO批次，此适配不可用于必须接收完整批次的未封装原生TUN。
- TUN发送只在上批交给WG后取下一批缓冲，等待TX缓冲可被Device.Close取消。keepalive使用单独4个槽，TX耗尽不阻止握手确认；其槽满时不分配、不阻塞，仍尝试推进已暂存包。
- 接收保留当前读缓冲；没有下一块RX缓冲时丢弃当前入站包并复用原缓冲，不在收包循环中等待耗尽的池，防止收发相互锁死。控制设备有独立RX/TX预算，不受业务设备占用。
- 元素及容器也固定预分配：出站各`2*(TxBuffers+KeepaliveBuffers+MaxPeers)+BatchSize`个，入站各`2*(RxBuffers+MaxPeers)+BatchSize`个。额外余量覆盖缓冲归还早于元素/容器归还、每peer顺序处理及nonce边界拆容器的瞬时持有。
- 等待可用keypair的staged包，跨peer共用不超过`(TxBuffers-BatchSize)/2`的信用；额度满时有界丢弃、仍触发握手，释放或进入加密发送时返还信用。原TX总预算不变，保留正常会话和下一次TUN读取的空间；keypair在入队后过期也重新核对额度。业务、控制、E2E设备都适用。
- 每设备peer上限沿用transport已有16/128上限，可在角色变化时按配置调整；E2E另按64/128调整。两类设备各队列与每worker容量固定。member数据/控制每类worker最多2/1个，relay最多4/2个，不再按主机全部CPU无限放大。
- `IsUnderLoad`（`device.go`）的阈值是握手队列实际容量的1/8、至少1（`handshakeLoad`）：上游1024深时仍为128；固定资源模式深度4时为1。原写死的`QueueHandshakeSize/8`（128）在深度4下永远达不到，cookie与按IP限速（`receive.go`）从不启用。
- `SendStagedPackets`（`send.go`）经`queueOutbound`把容器交给peer的顺序发送队列：普通WG仍阻塞等待；固定资源模式下最多等`outboundStallWait`（500 ms；100/250 ms时`mesh`的`TestLoad`里一次TCP恢复造成约0.3 s的分片UDP丢失），超时丢弃并把该peer记为stalled，其后该peer队列仍满时直接丢弃、不再等待，队列一有空位即恢复等待。否则一条写入阻塞到发送期限（5 s）的流路径会让TUN读取停在这个peer上，整台设备的发送随之停住（`mesh`的`TestStalledStreamPeer`，`OBLAB_STALL=1`）。丢弃发生在nonce分配之后，接收端重放窗口容许空缺。

| profile | data TX/RX/keepalive | control TX/RX/keepalive | 两设备消息槽合计 | 按每槽64 KiB计算 |
|---|---:|---:|---:|---:|
| member | 32/64/4 | 8/16/4 | 128 | 8 MiB |
| relay | 64/128/4 | 16/32/4 | 248 | 15.5 MiB |

Linux/macOS数组为65535字节，按64 KiB页分配取上界；Windows数组更小，仍用同一上界。该硬上限是WG消息存储，不是整个进程RSS硬限制：队列元数据、peer密码状态、栈、调度器、transport缓存及Go运行时另外占用，内核socket内存不计入RSS；不能用该数字冒充160/256 MiB整进程上限。

## transport如何使用

`node/mesh/transport/` 的plane0 Bind与plane1 PlaneBind实现认证及显式发送接口，共享物理socket和资源。Endpoint包含本地path ID/连接generation；认证通知验证配置中的业务/控制公钥归属一致，连接已断开或代次过期时拒绝。health用控制TUN的逐包元数据关联nonce/challenge，不根据最近一次握手或最后端点认定路径成功。

控制WG实例必须有单独私钥/会话，控制源地址只映射获准peer。业务和控制实例不共享重放窗口。配置注册、控制载荷大小/频率、nonce/mesh/revision、prepare/commit及业务路由授权仍是上层责任。

## 升级步骤

1. 明确升级目标版本，保持本地补丁之外的源码与该版本一致；保留LICENSE与完整版本出处，不能直接覆盖补丁后宣称兼容。
2. 对比上述文件。重新定位握手认证、AEAD、反重放、AllowedIPs、异步发送与回池位置，确保通知时序和元数据生命周期仍成立；保留显式握手目标在节流前记录、默认重试复用、完成/停止/废弃时清理的顺序。
3. 密码/窗口变化不是本补丁的授权范围；若上游改变这些路径，重新评估并报告，不机械移植。
4. 从node主模块按只读依赖环境验证，避免上游模块测试顺带变更主模块依赖。

```sh
cd node
export HOME=/root GOTOOLCHAIN=local GOFLAGS=-mod=readonly GOCACHE=/tmp/go-build
go test -race ./...
go test -race golang.zx2c4.com/wireguard/device golang.zx2c4.com/wireguard/conn golang.zx2c4.com/wireguard/replay
GOOS=linux GOARCH=amd64 go vet ./... ./cmd/...
GOOS=windows GOARCH=amd64 go vet ./... ./cmd/...
GOOS=darwin GOARCH=arm64 go vet ./... ./cmd/...
go build ./...
go test -run TestTwoPlaneMemoryBudget -count=1 -v ./mesh/transport
```

## 必须保留的回归

测试实际写在 `node/mesh/transport/`：

- `TestAuthenticatedProvenanceReplayAndWrongIdentity`：真实WG加密数据的重复/篡改既不交付明文，也不增加认证回调次数；控制平面不接受业务key，已有路径不接受另一合法peer的key，撤销后不复活。
- `TestControlReplaySpaceSurvivesBusyData`：保留一份真实控制密文，推进8300份业务包超过业务重放窗口，再交付控制密文仍有效。
- `TestAuthenticatedOneWayNewPathSurvivesDeadline`：使用已有密码会话在另一socket单向发送，接收方没有向该socket回包，超过原10秒期限仍可认证接收。
- `TestMultipathControlPrepareCommit`：同一控制会话沿TCP与TLS路径探测，逐包元数据指向实际路径；prepare不改变活动业务，提交后才能切换；旧代次不可提交，释放与撤销完整回收。
- `TestHundredFailedProbesPreserveActivePath`、`TestFailedPrepareReclaimsTasks`：100次失败UDP探测及100次失败TCP准备，不关闭活动连接或残留拨号任务。
- `TestHotPathSwitchTransportLatency`：20次已就绪路径提交与双向业务恢复。此测试不代替health故障检测/归属传播的全链路8秒指标。
- `TestControlBindCloseDoesNotCloseBusiness`：控制Device Down/Up不关闭业务物理I/O。
- `TestUncommittedDataHandshakeConfirmsOnRequestedPath`：未Commit时完成数据握手与空确认，被动端handshake完成；没有任何先发业务的成员在提交后直接收到首个反向包。
- `TestKeepaliveToProbesDataPathWithoutSwitching`：已确认会话经备用物理路径发送空认证包，不重置握手、不改变活动业务路径、不向业务TUN交付载荷。
- `TestExplicitHandshakeRetryRetainsUncommittedPath`：真实WG首包丢失、本地ErrBackpressure、显式调用被已有节流拦下三种情况下，无第二次Warm仍由原定时器在同generation上完成握手；未Commit业务继续拒绝。
- `TestFailedDataProbesKeepActiveTCPAndSession`：活动TCP旁100次失败UDP空业务会话探测，不换连接、不改active、不改变双端握手时间，双向业务持续正常。
- `device/openbear_test.go`的`TestHandshakeEndpointLifecycle`：节流期保存目标、普通重试不清目标；完成、停止、清除密钥、过期和重试耗尽均清理。
- `device/resources_test.go`的`TestMeshResourcesHavePeerIndependentCaps`：增加到peer上限不增加消息数组，耗尽TX后禁止溢出分配且RX/keepalive仍可用，Close取消等待；设备/peer队列容量确实为4。
- `device/resources_test.go`的`TestHandshakeLoadFollowsQueueCapacity`、`TestStalledPeerQueueDropsInsteadOfBlocking`：过载阈值随队列容量；满队列先等`outboundStallWait`再丢，stalled期间不等，有空位后恢复。
- `TestTwoPlaneFixedBufferBudget`：填满member/relay逻辑peer注册额度后，两plane合计仍为128/248个消息槽。
- 原A2三协议、迟到UDP不改主动传输、NAT漫游、pin失败、证书轮换、监听回滚和有界缓冲测试。
- 非race的Linux RSS测试统计同进程两个节点/四个WG实例，包含控制实例。race影子内存不作为产品RSS结果。

资源模式经主会话明确授权，仅对mesh的两个WG设备固定消息池/元数据池、队列和worker数；没有修改进程全局Go内存限制。小规模回归通过不意味着任意网格规模/压力下已满足160/256 MiB整进程RSS，仍须结合调度器、transport及其他节点功能验收。
