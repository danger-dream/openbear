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
- `SendMetadata{Endpoint, ID, Peer}` 与 `MetadataReader.ReadWithMetadata(bufs,sizes,offset,[]SendMetadata)`：TUN给单包附加本地目标路径和关联ID，随异步加密保留。`Peer`非零时是TUN为本机的包指定的下一跳peer公钥（mesh固定路径：同一目的地按来源和入口走不同下一跳），只在本机产生，不来自网络。
- `MetadataSender.SendWithMetadata(bufs, publicKey, SendMetadata)`：Bind收到指定路径、真正由AllowedIPs选中的peer公钥及关联ID，可验证路径归属并记录写入完成。ID只在本机使用。
- 大包（TCP直连大包，Linux）：`DeviceLimits`的`LargeBufferSize`、`LargeBatchSize`、`LargeTxBuffers`、`LargeRxBuffers`描述第二个缓冲池；`LargeReceiver.LargeReceiveIndex()`指出Bind哪个接收函数送来大帧，`LargeReader.ReadLarge(...)`是TUN交出放不进普通缓冲的包的第二个读取入口。三者都不实现时行为不变。
- `LargeSwapReader.ReadLargeSwap(...)`：可选的大包读取接口；仅在TUN也实现`LargeReader`且设备启用了大包TX池时由大包读取例程使用。它可将返回的buffer替换为**同尺寸、独占**的完整backing，设备接管后才进入加密或错误清理；未实现时继续使用`ReadLarge`。被换下的原buffer从此归读取方，设备不再保留引用。mesh只对可确认来源的TUN独占大包槽，在offset、buffer尺寸及WG头部、padding和AEAD标签所需空间均匹配时交接；中继/E2E等其他来源、槽或空间不匹配时仍拷贝，放不进目标缓冲的包仍按原路径丢弃。普通包和控制面不使用此交接。mesh把换下的buffer放进有上限的空闲表，作为pump下一次TUN读槽，表空才新分配，表满交给Go回收。
- `LargeSwapReceiver.LargeSwapReceive() ReceiveFunc`：可选，Open之后调用，给出`LargeReceiveIndex`所指接收函数的交换版本；返回nil或Bind未实现时用原函数。交换版本可把`packets[i]`换成**长度与容量都相同、独占**的buffer（消息在其开头），并留下被换下的那块；调用方对返回计数内的每一项（返回错误时也一样），在使用、排队或归还之前接入原池buffer。mesh只在TCP流上长度大于8192字节、不超过9472字节的数据面帧读入大包空闲缓冲，在交给WG时目标buffer长度与容量都恰为9472字节才互换，换下的buffer回Bind的空闲表；其他情况（目标尺寸不符、TLS、控制面plane 1、未知plane、UDP、8192字节及以下的帧、设备未接管）仍复制或保持原分配，大包缓冲随即回空闲表。

### `device/openbear.go`（新增）

- 向可选Bind通知认证结果。
- 单包显式路径发送保留设备关闭检查、net锁和tx统计。没有显式Endpoint时仍调用原SendBuffers；Bind未实现MetadataSender时可直接用原Bind.Send指定Endpoint。

### `device/receive.go`

- 握手init/response在原 `ConsumeMessageInitiation/Response` 成功后通知；握手自己的认证及时间戳防重放语义不变。
- type4在AEAD成功且 `replayFilter.ValidateCounter` 成功后通知，覆盖空keepalive和单向流量。无效/重复密文不触发通知。
- type4认证通知只证明peer身份及包的新鲜性，**不证明IP源地址获得业务授权**；源验证仍在原位置，AllowedIPs拒绝时由 `device/openbear.go` 的 `sourceAllowed` 询问TUN实现的SourceAuthorizer，只有通过者交给TUN。
- 启用适配的Bind，其握手响应沿本次请求Endpoint发送；收到type2并成功派生keypair之后，空加密确认包也沿该响应的原Endpoint发送，不依赖尚未Commit的业务逻辑端点。未启用适配仍用原响应/keepalive路径行为。
- 解密后WriteWithMetadata与bufs一一对应；不通过最后一个漫游端点或缓冲指针推断路径。
- `RoutineReceiveIncoming`增加`adopt`参数，仅大包接收函数使用`LargeSwapReceive`时为真：每次recv返回后（包括返回错误时），先把前count项`bufs[i]`写入`bufsArrs[i].Data`，再做长度检查、建元素、flush或归还。`messageBuffer`对象身份、按对象建立的owner映射、回池路径、池数量和批次不变；被换下的slice由Bind持有。未互换项回写的是原slice，等同不变。解密、重放检查、顺序接收和`ReceiveDrops`计数不变。

### `device/send.go`

- `QueueOutboundElement` 保存单包SendMetadata，回池时清空，避免复用污染/引用保留。
- TUN实现MetadataReader时逐包读取；每次读取前清空metadata数组。
- 包的`SendMetadata.Peer`非零时，先照原样按目的地址查AllowedIPs，查得到peer才改用`LookupPeer(Peer)`的peer；那个peer已不在设备上就丢弃这个包。AllowedIPs查不到时仍丢弃，指定peer不会让设备发送原本没有路由的包。
- 有显式路径的批次按原顺序逐包发送；普通批次仍按原SendBuffers方式批量发送。密码计算/nonce及队列顺序不变。
- 大包读取例程仍先从large TX池取得element及buffer；若TUN支持`LargeSwapReader`，读取后将返回的同尺寸独占backing写入该element的`messageBuffer.Data`，即使本次读取也返回错误，也先接入返回的那些buffer，再处理数据或错误清理。被替换的backing不再由element或设备持有，归读取方所有（mesh放回TUN读槽空闲表）；`messageBuffer`对象身份、按对象建立的owner映射、归还路径和固定池限额不变，不让调度队列预占WG池。后续padding及Seal仍使用该element的同一backing。
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
- 批次由`DeviceLimits.BatchSize`决定（mesh业务设备32、控制设备1），每条设备/peer队列深度4；WG加密/发送背压在TX缓冲用尽后阻止下一次meshTUN.Read，让上游调度器承担排队及CoDel。meshTUN自己的OS读取泵保留原GSO批次。
- `MessageBufferSize`非零且小于65535时，TX、RX池的缓冲按这个大小分配（keepalive仍为65535）：已知MTU的设备可以在同样内存里放更多缓冲。TUN读取的包若放不下“填充后密文+AEAD标签”（`len(buf)-offset-16-15`），在加密前丢弃，不让Seal在池外分配。
- `PendingLimit`非零时，等待会话的待发包信用取它与`(TxBuffers-BatchSize)/2`中较小的一个。
- 大包池（方案A双池）：`LargeBufferSize`非零且不超过65535、`LargeTxBuffers`>0、`LargeRxBuffers`>1时，另分配大包TX、RX两个池，缓冲按`LargeBufferSize`；原2 KiB池和批次32不变。TUN实现`LargeReader`时另起一个读取例程`routineReadLargeFromTUN`，从大包TX池取缓冲，每次最多`LargeBatchSize`个；Bind实现`LargeReceiver`时，它指定的那个接收函数只从大包RX池取缓冲，批次不超过`LargeBatchSize`。大包缓冲归还只回大包池，不与其他池互借。出站、入站元素及容器的预分配数各加`2*LargeTxBuffers+LargeBatchSize`、`2*LargeRxBuffers+LargeBatchSize`。mesh只在Linux上给业务设备开它：64 TX＋96 RX（relay 128＋192）个9472字节，批次8，member多1.45 MiB、relay多2.89 MiB。原来的16＋24在单流超过1 Gbit/s时接收池耗尽。
- `BindUpdate`（`device.go`）：Bind实现`LargeSwapReceiver`、`LargeReceiveIndex`有效且大包RX池存在时，大包接收例程改用`LargeSwapReceive()`并以`adopt`运行；返回nil或任一条件不满足时与原来相同。其他接收函数不变。
- `PriorityRxBuffers`从RxBuffers中划出给Bind用`conn.PriorityReceiver.PriorityReceiveIndex()`指定的那个接收函数（mesh的握手紧急队列）：该接收例程只从这个池取缓冲，批次不超过池大小减1，这样数据占满其余RX缓冲时握手仍能收进来。不实现该接口的Bind行为不变。
- TUN发送只在上批交给WG后取下一批缓冲，等待TX缓冲可被Device.Close取消。keepalive使用单独4个槽，TX耗尽不阻止握手确认；其槽满时不分配、不阻塞，仍尝试推进已暂存包。
- 接收保留当前读缓冲。type4数据包没有下一块RX缓冲时，先把这一批已收的包交给解密与顺序接收，再等缓冲归还，不丢弃Bind已交出的包：否则TCP/TLS路径上Bind的“队列满就等待”失效，包在这里静默丢掉，内层TCP大量重传（跨洲际高延迟路径实测约13%）。收包循环原本就会在交给peer入站队列时等顺序接收腾位，等缓冲依赖的是同一个顺序接收。顺序接收在需要换密钥时会发握手（`keepKeyFreshReceiving`→`SendHandshakeInitiation`），要拿`device.net`读锁；而`BindUpdate`/`BindClose`持写锁等收包例程退出，所以等缓冲还要能被解绑打断：`closeBindLocked`先关掉本次绑定的`net.unbinding`，`waitReceiveBuffer`随即返回nil，收包例程退出，否则满池时重绑或关闭会三方互等。握手消息仍在没有缓冲时丢弃（握手另有优先池）。`Device.ReceiveDrops()`统计接收路径上别处不计的丢弃：握手缺缓冲、解密失败、重放、keypair缺失或过期。控制设备有独立RX/TX预算，不受业务设备占用。
- 元素及容器也固定预分配：出站各`2*(TxBuffers+KeepaliveBuffers+MaxPeers)+BatchSize`个，入站各`2*(RxBuffers+MaxPeers)+BatchSize`个。额外余量覆盖缓冲归还早于元素/容器归还、每peer顺序处理及nonce边界拆容器的瞬时持有。
- 等待可用keypair的staged包，跨peer共用不超过`(TxBuffers-BatchSize)/2`的信用；额度满时有界丢弃、仍触发握手，释放或进入加密发送时返还信用。原TX总预算不变，保留正常会话和下一次TUN读取的空间；keypair在入队后过期也重新核对额度。业务、控制、E2E设备都适用。
- 每设备peer上限沿用transport已有16/128上限，可在角色变化时按配置调整；E2E另按64/128调整。两类设备各队列与每worker容量固定。member数据/控制每类worker最多2/1个，relay最多4/2个，不再按主机全部CPU无限放大。
- `IsUnderLoad`（`device.go`）的阈值是握手队列实际容量的1/8、至少1（`handshakeLoad`）：上游1024深时仍为128；固定资源模式深度4时为1。原写死的`QueueHandshakeSize/8`（128）在深度4下永远达不到，cookie与按IP限速（`receive.go`）从不启用。
- `SendStagedPackets`（`send.go`）经`queueOutbound`把容器交给peer的顺序发送队列：普通WG仍阻塞等待；固定资源模式下最多等`outboundStallWait`（500 ms；100/250 ms时`mesh`的`TestLoad`里一次TCP恢复造成约0.3 s的分片UDP丢失），超时丢弃并把该peer记为stalled，其后该peer队列仍满时直接丢弃、不再等待，队列一有空位即恢复等待。否则一条写入阻塞到发送期限（5 s）的流路径会让TUN读取停在这个peer上，整台设备的发送随之停住（`mesh`的`TestStalledStreamPeer`，`OBLAB_STALL=1`）。丢弃发生在nonce分配之后，接收端重放窗口容许空缺。

| profile | data TX/RX（2 KiB）/keepalive | control TX/RX/keepalive | 合计 | 批次1、全部64 KiB时 |
|---|---:|---:|---:|---:|
| member | 224/384/4 | 8/16/4 | 约3.2 MiB | 8 MiB |
| relay | 448/768/4 | 16/32/4 | 约5.9 MiB | 15.5 MiB |

Linux上加上大包池：member约4.63 MiB、relay约8.77 MiB；再计入transport Bind与mesh TUN各最多`transport.LargeSpares`（64）个9472字节的大包空闲缓冲（两档相同，合计1.16 MiB），member约5.79 MiB、relay约9.92 MiB，仍在8/15.5 MiB内（`TestTwoPlaneFixedBufferBudget`的`large`子测试）。空闲缓冲只在启用大包队列时存在。

网络MTU不超过1420时业务设备用2 KiB缓冲；更大的MTU（只有测试使用）仍是32/64（relay 64/128）个65535字节缓冲、批次8。65535字节的缓冲按64 KiB计。该硬上限是WG消息存储，不是整个进程RSS硬限制：队列元数据、peer密码状态、栈、调度器、transport缓存及Go运行时另外占用，内核socket内存不计入RSS；不能用该数字冒充160/256 MiB整进程上限。

### `tun/tun_linux.go`、`tun/offload_linux.go`、`tun/checksum_fast_*`

- 带virtio头的TUN写入批次在2～32个包、且缓冲容量不足64 KiB时（如来自上面2 KiB的池），先拷进一块按需分配的2 MiB工作区（32×65535，受writeOpMu保护）再做GRO，否则合并后的包长不出原缓冲，GRO实际失效。调用方的缓冲不被修改。
- TCP的GSO拆分校验和与GRO前的校验和验证改用`tcpPacketChecksum`：256字节以上、CPU有AVX/AVX2/BMI2时用tailscale/wireguard-go 88b11b4a的SIMD实现（MIT，`LICENSE.checksum-upstream`，生成的汇编原样保留），否则用原实现；UDP不变。结果与原实现逐位一致（`checksum_fast_linux_test.go`）。

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
- `device/resources_test.go`的`TestBindCloseWhileWaitingForABuffer`：数据池只有一个收包例程、缓冲全被拿着时，`BindClose`仍立即返回，重绑后照常收发。
- `device/resources_test.go`的`TestMeshResourcesHavePeerIndependentCaps`：增加到peer上限不增加消息数组，耗尽TX后禁止溢出分配且RX/keepalive仍可用，Close取消等待；设备/peer队列容量确实为4。
- `device/resources_test.go`的`TestHandshakeLoadFollowsQueueCapacity`、`TestStalledPeerQueueDropsInsteadOfBlocking`：过载阈值随队列容量；满队列先等`outboundStallWait`再丢，stalled期间不等，有空位后恢复。
- `TestTwoPlaneFixedBufferBudget`：填满member/relay逻辑peer注册额度后，两plane的消息存储（含大包池及两份大包空闲缓冲上限）不超过原来128/248个64 KiB槽的8/15.5 MiB。
- `device/resources_test.go`的`TestLargePoolApart`、`TestNoLargePoolWithoutLimits`：大包池独立分配与回收，大包接收函数只用它、批次受限；没有大包限额时一切照旧。
- `device/resources_test.go`的`TestReceiveWaitsForABuffer`：接收TUN暂不读取、RX池被下游占满时突发60个包，读取恢复后全部送达，`ReceiveDrops`为零（旧行为丢46个）。
- 根模块`node/mesh/large_swap_test.go`的`TestLargeSwapWireGuardSend`实际运行WG读取、异步加密、发送和接收：单个large TX槽反复交接，检查数据和顺序，以及WG换下的buffer逐包回作TUN读槽（只首包新分配），并覆盖发送错误后的复用、peer移除重建及并发突发恢复。`TestLargeSendSlotsRecycled`、`TestLargeSendSparesBounded`覆盖读槽回收命中、交出的包不再被后续读取覆盖、表空新分配与空闲表上限。`TestLargeSwapOwnershipAndFallback`、`TestLargeSwapFallbacks`、`TestLargeSwapHeldAndQueueDrop`和`TestQueueRepresentationSizeAndMarker`分别覆盖交接与复制回退、held/队列丢弃以及不增加每项队列布局和普通包标记隔离。这些是根模块测试，不应计为已运行嵌套WG模块的独立测试。
- 根模块`node/mesh/transport/spares_test.go`的`TestLargeReceiveBuffersThroughWireGuard`实际运行TCP路径上WG的加密、Bind读帧、大包接收函数互换和解密写TUN：接收设备只有2个大包RX buffer、批次1，逐包、伴随反向流量的突发、TCP重连、Bind关闭重开及peer移除重建后，大包内容和顺序正确，全部走互换且没有复制，解密失败和重放计数为0。`TestLargeReceiveSwapsAndReusesBuffers`、`TestLargeReceiveFallsBackToCopying`、`TestLargeReceiveBuffersOnlyForLargeFrames`、`TestLargeReceiveSparesBounded`、`TestLargeReceiveBuffersGivenBackOnDrops`、`TestLargeReceiveBufferGivenBackWhenAStreamEnds`分别覆盖互换和不别名、各类复制回退、只对大帧使用且队列项不变、空闲表上限、retired/路径限额/队列满/stale/tooLong/Close丢弃时的一次归还，以及半帧和流关闭后的归还。这些同样是根模块测试。
- 原A2三协议、迟到UDP不改主动传输、NAT漫游、pin失败、证书轮换、监听回滚和有界缓冲测试。
- 非race的Linux RSS测试统计同进程两个节点/四个WG实例，包含控制实例。race影子内存不作为产品RSS结果。

资源模式经主会话明确授权，仅对mesh的两个WG设备固定消息池/元数据池、队列和worker数；没有修改进程全局Go内存限制。小规模回归通过不意味着任意网格规模/压力下已满足160/256 MiB整进程RSS，仍须结合调度器、transport及其他节点功能验收。
