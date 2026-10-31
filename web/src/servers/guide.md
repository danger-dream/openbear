# 服务器接入说明

每台服务器上装一个小程序“节点”，它会主动连回 OpenBear：上报运行状态、提供网页终端、执行命令。服务器不需要开放任何端口。

## 第一步：准备接入点

接入点是节点连回 OpenBear 用的地址，在「设置 → 接入点」里添加，填好后点「测试」。测试会用这个地址连一次 OpenBear，确认证书有效、转发正确、连到的是这台 OpenBear。

按你的情况选一种。

### 情况一：OpenBear 已经有 https 公网地址

例如你平时用 `https://bear.example.com` 打开控制台，直接把这个地址添加为接入点即可，不用做别的。

节点只会访问 `/api/server-nodes/` 开头的路径，这些路径只认节点自己的密钥。

### 情况二：控制台不对公网开放

需要准备三样东西：

- 一个域名，例如 `node.example.com`，解析到 OpenBear 所在的服务器；
- 这个域名的 https 证书；
- 一个反向代理，**只转发 `/api/server-nodes/`**，其他路径一律返回 404，这样控制台本身仍然不会暴露。

用 Caddy 最简单，它会自动申请证书：

```
node.example.com {
    handle /api/server-nodes/* {
        reverse_proxy 127.0.0.1:18961
    }
    respond 404
}
```

用 Nginx 或 OpenResty 时，在站点里加上：

```
location ^~ /api/server-nodes/ {
    proxy_pass http://127.0.0.1:18961;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 3600s;
    proxy_buffering off;
}
location / { return 404; }
```

`18961` 换成 OpenBear 实际监听的端口。

### 情况三：国内服务器连海外的 OpenBear 很慢或不稳

找一台到国内线路好的服务器（例如 CN2 GIA）做中转，再加一个“中转”接入点。中转机按下面两种情况选一种做法。

**中转机上已经有网站**（装了 Nginx、OpenResty 或 1Panel）：

1. 加一个域名，例如 `node-cn.example.com`，解析到**中转机**的 IP，并为它配好 https 证书。
2. 在中转机上照上面“情况二”的反向代理写法加一个站点，同样只转发 `/api/server-nodes/`，`proxy_pass` 写 OpenBear 的地址，例如 `https://node.example.com`。如果中转机和 OpenBear 之间有 WireGuard，写内网地址更好。
3. 把 `https://node-cn.example.com` 添加为接入点，地址不用带端口。

**中转机是空的**：

1. 用 realm 把一个端口原样转发到 OpenBear 服务器的 443。转发的是加密流量，中转机不需要证书，也看不到内容。空机器的 443 没有被占用，可以直接监听 443：

   ```
   [[endpoints]]
   listen = "0.0.0.0:443"
   remote = "OpenBear服务器IP:443"
   ```

2. 加一个域名，例如 `node-cn.example.com`，解析到**中转机**的 IP。这个域名要包含在 **OpenBear 服务器**的证书里，用通配符证书 `*.example.com` 最省事。
3. 把 `https://node-cn.example.com` 添加为接入点。如果中转机的 443 已被占用、改用了别的端口，地址里要带上这个端口。

### 内网服务器

WireGuard 或局域网里的机器，可以直接用 `http://内网IP:端口`。只有内网地址和本机允许用 http，公网地址必须用 https。

## 第二步：添加服务器

在「添加服务器」里有四种方式：

| 方式 | 适合 | 怎么做 |
|---|---|---|
| SSH 自动安装 | 能 SSH 登录的服务器 | 填 SSH 信息后保存，OpenBear 会连上去装好，并逐步显示进度 |
| 批量 SSH | 很多台用同一把密钥的服务器 | 每行一台，格式是“名称 地址 端口”，一次装完 |
| 安装命令 | 没有 SSH，或在 NAT 后面的机器 | 复制命令，到服务器上用 root 执行。命令 30 分钟内有效，只能用一次 |
| 注册码 | 经常要加新机器 | 一条命令可以装任意多台，按主机名自动建档。可以设有效期和次数，随时作废，有新机器加入会通知你。像是已有服务器重装后接入的（机器 ID 和主机名相同），先作为新服务器出现，由你确认接管原记录、单独保留或拒绝 |

每台服务器都要选一个**首选接入点**，还可以选一个**备用**。首选连续连不上时，节点自动换到备用，之后每 10 分钟检查一次首选，恢复了就切回来。修改接入点后，在线的节点会先确认能连上新地址，再切换过去。

## 安全

- **加密与身份校验**：节点和 OpenBear 之间端到端加密，双方每次连接都会核对对方身份。反向代理和中转机只能看到密文；即使有人拿到了伪造的证书，节点也只认注册时记下的那台 OpenBear。
- **关闭能力**：在安装命令末尾加 `--no-exec`，可以禁止命令执行、文件读写和在线更新；加 `--no-terminal`，可以禁止网页终端。
- **保护控制台账号**：能登录控制台的人，就能控制所有装了节点的服务器。

## 常见问题

- **安装停在“等待上线”**：说明这台服务器连不上所选的接入点。在服务器上执行 `curl -I https://接入点地址/api/server-nodes/v1/hello`，能看到 `200` 才是通的。
- **测试提示“证书无效”**：检查证书是否包含这个域名、有没有过期。中转用的域名也要在证书里。
- **测试提示“没有转发到 OpenBear”**：反向代理少转发了 `/api/server-nodes/`，或者转发到了别的地方。
- **卸载**：在服务器详情里选「移除节点」并勾选“同时卸载”，或者在服务器上执行 `openbear-node uninstall`。
