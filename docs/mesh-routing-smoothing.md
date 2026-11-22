# 组网选路平滑改造规格（任务 #1.3）

定稿：2026-11-20。决策人：老大授权由主控（Claude）定全部技术取舍。本文件是交给实现模型的完整规格，实现完成后由主控审查。

## 0. 背景与目标

现象：11-20 升级重启后约 7.5 小时内，每台中转到 29 个中转目的地的路由切换合计 43–1221 次。节点最近事件里有 1706 次“切走又切回”，29% 在 10 秒内切回，85% 由“更便宜”触发，因断线切换的只有 5%。

根因：

1. 丢包估计太敏感：`min(最近10, 最近30 个探测的丢包率) × 1000`。丢 1 个探测包，代价就 +33ms，持续 10 秒。
2. 防抖是互不相干的规则：5ms 切换门槛、通告前 2ms/5 秒保持、少一跳规则（3ms + 保持）、事后防抖 60–480 秒、线路扣留加倍。它们都用同一份通告代价，所以全网会同时切。

目标：继续一直选最优，防抖只用一个连续机制（Babel 的平滑度量，RFC 8966 附录 A.3、RFC 9616 §4）。具体是：

- 偶发丢包和小抖动不引起换路；
- 明显、持续更好的路能很快用上；
- 断线、严重劣化仍然秒级切走；
- 反复断线的线路吸引不到流量。

参考：RFC 8966 §3.6/附录 A.3，RFC 9616 §4，babeld `route.c`（`route_smoothed_metric`、`consider_route`），RON（SOSP 2001）§4，Tailscale PR #7940（1% 门槛），Tailscale `goodEnoughLatency`，ARPANET HN-SPF。

## 1. 已定决策

| 项 | 决定 | 理由 |
|---|---|---|
| 选路机制 | 每个（目的地, 下一跳）维护平滑代价；实时代价和平滑代价都更低（超过门槛）才换 | Babel 标准做法；切换快慢随“好多少、好多久”连续变化 |
| 切换门槛 | 便宜 1% 以上，且至少 1ms | 老大定 1%（Tailscale）；1ms 防止几毫秒的短路径因测量误差来回换 |
| 平滑速度 | 半衰期默认 15 秒，每步增量带 ±25% 随机 | babeld 默认 4 秒，但它的度量更平稳；随机化避免全网同步。最终值由回放在 {8,15,30,60} 里选（见 §5.2） |
| 新出现、刚恢复的路 | 平滑代价从“实时代价 + 500ms”起步 | Babel 新路由从无穷大的一半起步，ARPANET 新线路从最大代价起步；反复断线的线路吸引不到路由 |
| 丢包估计 | 长窗口 100 个探测、第 1 个丢包不计；最近 10 个里丢 ≥3 个时按短窗口计；另有半衰期 15 分钟的慢丢包率，每个丢包都计，断线期间的不计；三者取最大 | RON 用 100 个探测的平均；严重丢包几秒内反映出来。11-21 加慢丢包率：RFCHost-JP2 这类线路隔一两分钟丢一阵、平均约 0.8%，100 个探测的窗口在两阵之间清零，两小时内路由 21 次以“更优”切到它上面并碰上丢包；偶发单个丢包只加不到 1ms，低于切换门槛 |
| 丢包权重 | 保持 1000ms 对应 100%（1% ≈ 10ms） | 对大流量来说丢包比延迟伤害更大，不降权；噪声由估计方法处理 |
| 每跳代价 | 每条链路 +2ms | 取代“少一跳”专门规则（对应 RFC 9616 的 C） |
| 通告 | 通告实际代价，变化 ≥ max(1%, 0.5ms) 就发，去掉 5 秒保持 | Babel：稳定性只在选路时处理，不改通告的值 |
| 删除 | switchMs、shorterMs/Pct、advertMs/Pct、holdS、flapHoldS/MaxS、路由事后防抖、少一跳规则、线路 `held` 扣留加倍 | 被平滑机制取代 |
| 保留 | 防环路检查、断线和绕回立即切、不当中转（toll 优先）、自建路线、静态模式、线路 penalty 暂停/`kept`、MTU 窄线路 +50ms、bias | 和防抖无关，或者属于故障层保护 |
| 不做 | RON 快速复测、按业务分路由表、按流固定路径、RFC 9616 上限饱和、ARPANET 限制每次更新的变化幅度、Babel 序号可行距离、移动网络 MRO 自动调参 | 收益小或不适用；全网 droppedTtl=0 |

## 2. 算法规格（`node/mesh/routing`）

### 2.1 链路测量（metric.go）

- RTT：最近 5 个回应的中位数（不变）。抖动：中位数（不变）。“断开”：连丢 3 个，或路径消失（不变）。penalty、暂停、`kept`：不变。
- 删除 `held`（线路恢复后扣留、每次加倍）及 `heldMs`、`link.held`、`link.released`。它的作用由 §2.3 的起步惩罚承担。
- 丢包率 `L`：
  - `lost100` 是最近 100 个探测中丢失的个数，`lost10` 是最近 10 个中丢失的个数。
  - `L = max( max(0, lost100 − 1) / 100 , lost10 ≥ 3 ? lost10 / 10 : 0 , slowLoss )`。
  - 不足 100 个探测时，分母仍用 100。
  - `slowLoss`：每个探测按距上一个的时间加权（单步最多按 5 秒算）趋向 1（丢）或 0（回应），半衰期 15 分钟。线路进入断开时退回到最后一次回应时的值；断开期间的探测不计，断开的时长也不算衰减。
- 链路代价 `cost = rtt + jitterWeight·jitter + lossWeight·L + bias + narrow + hopCostMs`，至少 0.1；不是 up 时为无穷大。

### 2.2 通告（advert.go）

- 通告的就是 `cost`（保留 0.1ms 精度），不再有 advertMs/holdS 的保持。
- 状态变化（up/down/suppressed）立即发送，沿用现有的 100ms 合并。
- 只有代价变化时：`|cost − 上次通告| ≥ max(1% × 两者较大值, 0.5ms)` 才发，每个源最多每秒 1 次。
- 每 5 秒定期通告、两种线上格式、限速规则都不变。

### 2.3 选路（router.go）

对每个目的地 d、每个满足防环条件的邻居 N（现有的 loop-free 判断，不变）：

- 实时代价 `m_N` = 经 N 到 d 的路径代价（用通告代价，与现在的 `via` 相同），连同 toll 数。
- 平滑代价 `s_N`：
  - N 刚变为可用时（上一轮不可用，或第一次出现），设 `s_N = m_N + startPenaltyMs`。
  - 之后每轮：`s_N += (m_N − s_N) × (1 − 2^(−Δt/h)) × r`。h 是 `smoothHalfLifeS`，r 每步随机取 [0.75, 1.25]，结果不越过 `m_N`；`|m_N − s_N| < 0.1` 时直接取 `m_N`。
  - N 不可用（无穷大或不满足防环）时删除 `s_N`，下次可用时重新带惩罚起步。
- `margin(x) = max(switchPct% × x, switchMinMs)`。

每轮决定：

1. 当前下一跳不可用（断开、会绕回、不满足防环）：立即换到可用的 N。按 toll 少优先，其次 `m_N` 最小，再按现有平局规则（链路两端哈希）。原因写 `down` 或 `loop`，与现在相同。
2. 当前可用时，候选 b 是除当前外按（toll、`s_N`、平局规则）最优的邻居：
   - b 的 toll 更少：这个条件持续满 0.5 秒后换，原因 `better`；等待期间每次重算都用最新通告复核，`held` 为 `settling`。11-21 起：原来是立即换。一条经过不做中转节点的路恢复时，路上的中转会一起离开它，邻居听到一个的离开、还没听到另一个的，就会接过正在被放弃的旧路，两者互指形成环路，直到通告交叉（11-21 回放的 3 次环路都是这样）。
   - toll 相同，且 `m_b ≤ m_cur − margin(m_cur)` 并且 `s_b ≤ s_cur − margin(s_cur)`：换，原因 `better`。
   - 否则不换，并在状态里记录 `best`/`bestCost`/`held`：
     - `margin`：实时代价便宜得不够；
     - `settling`：实时代价已经够便宜，平滑代价还没追上；
     - `loop`、`transit`：沿用现有含义。
3. 删除 `shorter` 原因，以及 `hold`、`damped`、`penalty` 这几种 held 取值，连同 flap 计数。
4. 成员（Leaf）自己的选路用同一套规则。自建路线和静态模式不变。
5. 新增每条路由的计数 `flips`：一次 `better` 换路，如果回到 5 分钟内刚离开的那个下一跳，`flips` 加 1，开机后累计。

### 2.4 参数（Tuning / `routing.tuning`）

| 字段 | 默认 | 范围 | 说明 |
|---|---|---|---|
| smoothHalfLifeS | 15（回放后可改） | 1–300 | 平滑半衰期 |
| switchPct | 1 | 0–50 | 便宜超过多少百分比才换 |
| switchMinMs | 1 | 0–100 | 至少便宜多少毫秒 |
| hopCostMs | 2 | 0–50 | 每条链路的固定代价 |
| startPenaltyMs | 500 | 0–5000 | 新出现或刚恢复的路的起步惩罚 |
| jitterWeight, lossWeight | 2, 1000 | 不变 | |

旧字段（switchMs、shorterMs、shorterPct、advertMs、advertPct、holdS、flapHoldS、flapHoldMaxS）：新节点读到时忽略，不报错。

### 2.5 状态与事件

- `status.routing.routes[]`：新增 `smoothed`（当前下一跳的平滑代价）、`bestSmoothed`、`flips`；`held` 取值改为 margin/settling/loop/transit；`until` 删除。
- `status.routing.links[]`：删除 `heldMs`。
- 事件：`route.changed` 格式不变，原因只剩 down/loop/better；删除 `link.held`、`link.released`。

## 3. 兼容（N-1）

- 新节点功能标记 `mesh.routing.smooth`。
- 主控只给带这个标记的节点下发新参数。不带标记的节点（N-1）继续收旧格式的 tuning，主控用常量 `LEGACY_TUNING` 生成（取现行默认值 switchMs 5 等）。
- 新节点收到旧格式配置（没有新字段）时用新默认值。
- 通告的线上格式不变。新旧节点混跑时，靠两边都保留的 loop-free 检查保证不出环路（RFC 8966 §3.6：各节点用不同的选路策略可以互通，只要不选无穷大的路和不满足防环的路）。
- 主控的 `mesh_tuning_outdated` 警告改成提示“这些节点更新后才使用平滑选路”。

## 4. 主控与前端

- `app/servers/mesh_model.py`：`settings.routing` 校验新字段，旧字段读取时丢弃，下次保存时清掉。
- `app/servers/mesh_config.py`：按功能标记生成新或旧 tuning，`TUNING_FEATURE` 旁边加新标记。
- `app/servers/mesh_status.py`：`_routing` 清洗新字段，去掉 `heldMs`、`until`。
- `app/servers/mesh_trend.py`：新增 `route_flips` 列（启动时 ALTER，做法和 #1.2 的 route_switches 一样），按各路由 `flips` 的增量累计；接口字段 `routeFlips`。
- `app/tools/servers.py`：mesh_trend 汇总加 routeFlips；settings 说明、mesh_path 的 held 说明同步。
- 前端：
  - `MeshSettings.vue`：选路参数换成 §2.4 的 5 项。
  - `MeshNodeRoutes.vue`、`MeshLink.vue`：显示平滑代价，去掉扣留剩余时间。
  - `meshHelpers.js`：`HELD_TEXT` 增加 settling（“更好的路还在观察，平滑代价没追上”），删除 hold/damped/penalty；去掉 shorter、link.held、link.released 的文案。
  - `MeshTrend.vue`：标题行加“来回 N 次”。
  - 手机端按项目的手机端规则检查。
- 文档：`node/mesh/routing/README.md` 的 Parameters、Link metrics、Advertised cost、Choosing a next hop、Status and events 各节按本规格重写。

## 5. 测试方案

### 5.1 单元与场景测试（`go test -race ./mesh/routing`，固定时钟、确定性随机种子）

现有测试按新规则改写，删掉只对应已删规则的测试（shorter、flap damping、held）。新增：

| 场景 | 通过标准 |
|---|---|
| 当前路每 1000 个探测偶发丢 1 个，备选路贵 0.5% | 1 小时内 0 次换路 |
| 当前路每 100 个探测丢 1 个（持续 1%），备选路贵 0.5% | 换到备选路 1 次，之后不回切（11-21 起；之前要求 0 次） |
| 两条路相差 1%，RTT 各有 ±3ms 噪声 | 每小时 ≤ 1 次换路 |
| 当前路的链路断开 | 4 秒内切走 |
| 当前路突然丢包 30% | 15 秒内切走 |
| 当前路 RTT 突增 200ms | 10 秒内切走 |
| 新路持续便宜 10% / 3% / 0.5% | 分别在 60 秒内 / 180 秒内 / 始终不换 |
| 故障切走后原路恢复，比临时路便宜 40% / 2% | 分别在 60 秒内 / 5 分钟内切回 |
| 反复断线的线路（up 20 秒、down 20 秒，30 分钟） | 没有路由换到它上面 |
| 经过不做中转节点的路恢复，上游和两个邻居先后离开它，各自先听到不同的通告（11-21 回放的环路） | 不形成环路（TestTransitMoveWaitsForAdvertisements） |
| 只有经过不做中转节点的路时，另一条路恢复；等待期间当前路断开 | 满 0.5 秒才离开；断开则立即切换，原因 `down`（TestTransitMoveWaitsHalfASecondUnlessBroken） |
| 不当中转、自建路线、静态模式 | 现有测试全部通过 |
| 新旧路由器混跑（`TestRollingUpgrade`、收敛测试） | 不出环路 |
| 配置 | 旧格式 tuning 能读；新字段越界被拒（config_test） |
| 性能（bench_test，25 台中转） | 每轮计算耗时 ≤ 现在 × 1.2 |

### 5.2 真实数据回放（新增 `replay_real_test.go`）

- 数据：`workspace/artifacts/mesh-replay/mesh-replay-20261119-20261120.json.gz`，主控已导出。内容：
  - 11-18 21:17 到 11-20 21:35 的 `mesh_line_minute`，共 231 条线路、38 万行；
  - 修订版 756–776 的成员和中转线路；
  - 各节点最近的换路事件。
  - 字段说明见文件里的 `note`。
- 只取中转之间的线路（kind=relay），转成精简格式放进 `node/mesh/routing/testdata/`，压缩后 ≤ 3MB，转换脚本放 `scripts/`。
- 合成每秒探测：
  - RTT 以分钟均值为中心，在 [rtt_min, rtt_max] 内取三角分布；
  - 按分钟丢包率随机丢探测；
  - `known_s − up_s` 秒的断开，在这一分钟内合成为连续丢失；
  - 没有数据的分钟沿用上一分钟。
  - 随机种子固定。
- 拓扑：25 台中转取自修订版 776，不当中转取自文档；某一分钟有数据的线路，视为当时存在。
- 运行：所有中转的 Router 在进程内互发通告，投递延迟取链路 RTT 的一半，沿用 net_test/replay_test 的做法。分别跑现行代码加现行默认值，和新代码，再对新代码依次试半衰期 8/15/30/60 秒。
- 完整 48 小时回放用环境变量 `OBLAB_REPLAY=1` 开启，平时只跑其中 6 小时；完整回放必须在 3 分钟内跑完。
- 指标（按中转汇总，再汇总全网）：
  - better 换路次数、5 分钟内切回次数；
  - 按真实合成代价、沿实际转发路径算的时间加权平均路径代价；
  - 路由时间中，落在最优路径 3% 以内的比例；
  - 每次断开事件中，用到该链路的路由多久切走；
  - 转发环路出现的次数和时长。
- 报告写到 `workspace/artifacts/mesh-replay/report-<日期>.md`，附 JSON。

### 5.3 netns 实网测试（需要 root，`OBLAB_NETNS=1 OBLAB_ROUTING=1`）

- `TestRoutingNetnsTCPNoise`：标准从“任何路由最多换 2 次”收紧为 10 分钟内最多 1 次；T-B 断开仍要 5 秒内绕开。
- `TestRoutingNetnsFaults`、`TestRoutingNetnsNoLoops`（droppedTtl 必须为 0）、`TestRoutingNetnsRollingUpgrade`：全部通过。
- 环境跑不了时，在汇报里写明。

### 5.4 主控和前端

`.venv/bin/python scripts/check.py` 通过。节点构建用 `.venv/bin/python scripts/build_node.py`（会先跑 go test -race）。前端执行 `npm run build`。

## 6. 验收目标

回放（完整 48 小时，新代码选定的半衰期对比现行代码）：

1. better 换路次数减少 ≥ 90%；5 分钟内切回次数减少 ≥ 95%。
2. 时间加权平均路径代价不高于现行 +1%；路由时间中 ≥ 95% 落在最优路径 3% 以内。
3. 每次断开事件，受影响路由的切换耗时 ≤ 现行 +1 秒，且绝对值 ≤ 5 秒。
4. 环路次数不多于现行，单次 ≤ 2 秒。
5. 半衰期取满足第 1 条的最小值；第 3 条和 §5.1 的劣化场景必须同时满足。

上线后（需要老大授权重启主控、更新节点，由主控审查）：用 `routeSwitches`/`routeFlips` 趋势，对比上线前 24 小时，每台中转每小时换路次数 ≤ 10%，切回次数 ≤ 5%；断线切换仍在 5 秒内。

## 7. 交付边界

- 工作树里有其他会话未提交的改动（mesh CPU 优化，以及 #1.1、#1.2 的主控改动）。动手前先看 `git status` 和 `git diff`，在这些改动之上开发，不能回退或覆盖。
- 测试只用 check.py 和 build_node.py，不跑全量；不手工改 `node/VERSION`。
- 不 push、不打 tag、不发布；不重启 openbear.service、不更新节点，这些都等老大明确指示。
- 完成后汇报：改动文件清单、回放报告路径和关键数字、各测试结果，以及与本规格的每处偏差和理由。

## 8. 审查要点（主控审查时逐项看）

- §2.3 的切换条件是否严格是“实时和平滑都超过门槛”，断开和绕回是否不受平滑影响。
- 起步惩罚是否在“不可用变为可用”的每种情况下都生效，包括线路恢复和防环条件恢复。
- 随机化是否按节点、按步进行，而不是全网同一个种子。
- 通告的变化阈值和限速是否会造成消息风暴（看 bench 和回放里的通告次数）。
- 旧版本节点是否真的收到旧 tuning；混跑测试是否覆盖。
- 回放报告的数字是否达到 §6，是否能复现。
- 被删除的规则对应的测试和文档是否都清理干净。
