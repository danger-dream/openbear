# benchmark-package

服务器「评测」用到的第三方检测工具，由 OpenBear 固定版本、托管副本。运行时主控把对应版本推到节点上执行，不再从上游仓库现拉脚本或资源。

主控用哪个版本写死在 `app/servers/bench_catalog.py` 的 `PACKAGES` 里；换版本就是新建版本目录、改这一处，随主控一起发布。

## 目录

```
<工具>/
  recipe.json          怎么从上游生成一个版本：上游仓库、要复制的文件、要固定的二进制
  v0.1.0/
    manifest.json      上游提交、每个文件的 SHA256、二进制的下载地址和 SHA256
    openbear.patch     我们对上游的改动（没有改动的工具没有这个文件）
    LICENSE            上游许可证
    ...                运行时文件，路径和 manifest 的 files 一致
```

`main/ref/...` 对应上游仓库 `main` 分支的 `ref/` 目录：xykt 的脚本按 `<仓库地址>main/ref/<文件>` 取资源，补丁把仓库地址换成节点上的本地目录，路径原样保留。

二进制不进 git。manifest 记着下载地址和 SHA256，主控第一次用到时下载、校验、缓存在数据目录的 `benchmark-cache/`，再推给节点。缓存命中仍复核固定摘要，解包缓存也与已校验归档逐文件比对，损坏时重新获取或解包。

## 运行约定

节点上，每个版本放在 `/var/tmp/openbear-bench/<工具>-<版本>/`，每次运行有自己的输出目录。主控设置这些环境变量：

| 变量 | 含义 |
|---|---|
| `OB_BENCH_DIR` | 这个版本在节点上的目录 |
| `OB_BENCH_OUT` | 本次运行的输出目录，跑完由主控收走并删除 |
| `PATH` | 前面加上 `$OB_BENCH_DIR/bin`，脚本找到我们固定的 nexttrace、speedtest 等，就不会再下载安装 |

打过补丁的脚本把报告写到 `$OB_BENCH_OUT/report-<4|6>.ansi`，JSON 写到 `result-<4|6>.json`，生成了在线报告时链接写到 `link-<4|6>.txt`。独立 Geekbench 和硬件报告中的 GB5 输出都在进入实时与持久日志前去掉认领密钥。

## 各工具

| 工具 | 上游 | 许可证 | 我们的改动 |
|---|---|---|---|
| ip.check.place | xykt/IPQuality | AGPL-3.0 | 资源读本地；不计数、不显示广告；报告和 JSON 写到输出目录 |
| net.check.place | xykt/NetQuality | AGPL-3.0 | 同上；保留「能否访问 Google」的判断，用来选 STUN 服务器；nexttrace、speedtest、stun 用固定版本 |
| hardware.check.place | xykt/HardwareQuality | AGPL-3.0 | 同上；隐私模式下不上传跑分；只有生成在线报告时才复用固定 GB5 二进制到本包目录，不安装到系统；报告通过 HTTPS 限时上传 |
| tcpquality | ibsgss/TcpQuality | GPL-3.0 | 测速子进程在退出或收到停止信号时清理自己创建的计数链；测试节点列表仍由作者的服务提供 |
| service-unlock | shui1iao/service-unlock | AGPL-3.0-only（原有部分 MIT） | 无；用上游 v0.5.4 发布的二进制，manifest 记录对应源码提交 |
| geekbench5 | Primate Labs（闭源） | 官方许可 | `run.sh` 是我们写的：内存不足时临时 swap、检测 OOM、去掉认领密钥、读回成绩；Geekbench 本身从官方地址下载，不再分发 |

service-unlock 的二进制目前无法从源码逐字节复现（上游的编译参数和工具链版本不同），以发布版的 SHA256 为准。

## 更新一个工具

```bash
.venv/bin/python scripts/benchmark_package.py prepare ip.check.place --commit <上游提交>
# 在 workspace/benchmark-work/ip.check.place 里检查补丁是否还对得上，需要时修改
.venv/bin/python scripts/benchmark_package.py build ip.check.place v0.1.1
.venv/bin/python scripts/benchmark_package.py check
```

`prepare` 会把上一版的补丁打到新的上游提交上，对不上时停下等人处理。`build` 把工作区的改动存成新版本的 `openbear.patch`，复制运行时文件，重新固定二进制。之后在 `PACKAGES` 里改版本号，跑评测确认结果正常。

## 许可证

AGPL／GPL 允许保存、修改和再分发，要求保留许可证、注明改动、提供对应源码：脚本本身就是源码，改动在 `openbear.patch` 里，上游提交记在 manifest 里。这些工具都是在目标机器上独立运行的程序，不影响 OpenBear 自身代码的许可。
