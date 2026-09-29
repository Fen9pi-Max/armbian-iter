# 服务端文档

本文档面向部署和运维 `armbian-iter` 服务的管理员,覆盖部署、配置、目录布局、HTTP API
参考与常见问题。客户端(局域网内被升级的设备)请看 [client.md](client.md)。

## 1. 运行要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Linux(需要 root:服务内部使用 `losetup` / `mount`) |
| Python | ≥ 3.8,仅标准库,**无任何 pip 依赖** |
| 系统工具 | `losetup` `mount` `umount` `tar` `dpkg-deb` `hostname`(发行版自带) |
| 网络 | 监听 `0.0.0.0:8090`(可改),客户端需可访问 |
| 磁盘 | 空闲空间 ≥ 上传镜像体积 + 约 8 GB(解压与打包临时空间),服务会在接收前检查并拒绝(507) |

服务可在 x86 服务器上部署(为 RK3588 板卡分发内核),板型匹配通过配置 `fdtfile` 完成,
见 §3。

## 2. 安装与启动

```bash
sudo python3 app.py
```

首次启动自动:

1. 创建数据目录 `/srv/armbian-iter/{repo,pool,artifacts,logs,tmp}`;
2. 生成配置 `/etc/armbian-iter.conf`(权限 0600),内含随机管理 token;
3. 建立空仓库索引,监听 `0.0.0.0:8090`。

查看 token:

```bash
sudo cat /etc/armbian-iter.conf
# {"token": "xxxxxxxxxxxxxxxxxxxxxx", "port": 8090}
```

浏览器打开 `http://<服务器IP>:8090/`,弹出的 Basic 认证**用户名随意、密码填 token**。

### systemd 常驻

`/etc/systemd/system/armbian-iter.service`:

```ini
[Unit]
Description=Armbian Iter LAN apt repo
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/armbian-iter/app.py
Restart=on-failure
User=root

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now armbian-iter
```

## 3. 配置文件 `/etc/armbian-iter.conf`

JSON 格式,修改后重启服务生效。

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `token` | str | 首次启动随机生成 | 管理接口密码(Basic 认证,用户名任意)。置空字符串可关闭认证(**不建议**) |
| `port` | int | `8090` | HTTP 监听端口。改动后客户端源地址需同步修改 |
| `fdtfile` | str | 读服务器 `/boot/armbianEnv.txt` 的 `fdtfile=` | **目标板型**的设备树文件名,如 `rk3588s-skysi-x5.dtb`。服务器与目标板型不同(如 x86 服务器)时必须显式配置,否则镜像校验会失败 |
| `disable_selfpack` | bool | `false` | 为真时 `POST /api/selfpack` 返回 403。服务器自身是 x86 时建议开启,避免把 x86 内核打包进 arm64 仓库 |

路径也可用环境变量覆盖(供测试 / CI 使用沙箱目录,正常部署无需设置):

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `ITER_CONF` | `/etc/armbian-iter.conf` | 配置文件路径 |
| `ITER_BASE` | `/srv/armbian-iter` | 数据目录根(repo/artifacts/logs/tmp/quarantine) |

## 4. 目录布局

```
/etc/armbian-iter.conf             # 配置(0600,含 token)
/srv/armbian-iter/
├── repo/                          # apt 仓库根 = /apt/* 匿名只读区
│   ├── Packages / Packages.gz     # 包索引(原子替换更新)
│   ├── Release                    # 含 MD5/SHA1/SHA256 校验和;Architectures 按池内实际架构生成
│   ├── index-meta.json            # 池内包元数据(Web UI / /api/status 使用,含 kind: kernel/app)
│   └── pool/*.deb                 # 所有 deb 包(内核重打包产物 + 直传的应用包)
├── artifacts/<工件ID>/            # 每次上传一个目录
│   ├── meta.json                  # 状态机元数据
│   ├── image.img                  # 解出的镜像(处理完成后保留,可用于 reprocess)
│   └── debs/                      # 重打包产物(入池后即从工件移除)
├── quarantine/                    # 索引重建时发现损坏的 deb 自动移入此处,不参与索引
├── logs/service.log               # 滚动日志(5 MB × 3 份)
└── tmp/                           # 打包临时目录(用后即删)
```

## 5. 处理流水线

上传 `.tar.gz` / `.img` 后,后台线程按如下状态机处理(`meta.json` 的 `state` 字段,
Web UI 与 `/api/status` 实时可见):

```
processing ──► extracting(仅 .tar.gz:解出唯一 .img)──► repacking ──► done
     │                                                        │
     └────────────────────── 任意阶段异常 ──► error(记录 error 字段)──┘
```

`repacking` 阶段的安全校验与动作:

1. 解析镜像 MBR/GPT,定位第一个 Linux 分区(GPT 类型 GUID
   `0fc63daf-…` / `b921b045-…`,或 MBR 类型 `0x83`),`losetup -r` 只读挂载;
2. 校验镜像 `/boot` 下**有且仅有一个** `vmlinuz-*`(多内核镜像拒绝);
3. 校验镜像 `/boot/dtb-<kver>/` 下存在本配置 `fdtfile` 指定的 dtb,否则拒绝入池
   (防止刷入其他板型的镜像);
4. 读取镜像 `var/lib/dpkg/status` 与 `info/<pkg>.list`,将内核三件套
   `linux-image-edge-rockchip64`、`linux-dtb-edge-rockchip64`、`linux-headers-edge-rockchip64`
   重打包为 `.deb`(`dpkg-deb --build -Zzstd`):
   - 文件严格按 `.list` 清单归档(`--no-recursion`,不会混入其他包的文件);
   - 维护脚本 `preinst/postinst/prerm/postrm/triggers`、`conffiles` 原样带入并置可执行;
   - 版本号 = 镜像内原版本 `+<内核版本>`(如 `26.11.0-trunk+7.2.4`),已带后缀则不重复加;
   - `Installed-Size` 按实际重算,`Status`/`Conffiles` 字段剔除。
5. 入池(同名 deb 已存在则跳过),原子重建 `Packages` / `Packages.gz` / `Release`。

镜像在处理中**只读挂载**,失败不损伤工件;`error` 状态的工件可修复问题后调
`reprocess` 重试,或删除重传。

## 6. 认证

- `GET/HEAD /apt/**`:匿名只读(apt 客户端使用),路径被限制在仓库根内,防路径穿越;
- 其余一切接口与 Web UI:HTTP Basic 认证,**用户名任意,密码 = token**
  (常量时间比较,防时序侧信道)。

```bash
TOKEN=$(sudo jq -r .token /etc/armbian-iter.conf)
curl -u ":$TOKEN" http://127.0.0.1:8090/api/status
```

## 7. HTTP API 参考

以下示例均假设 `TOKEN` 已按上文取得,服务在 `127.0.0.1:8090`。

### GET `/api/status` — 总览状态

```bash
curl -u ":$TOKEN" http://127.0.0.1:8090/api/status
```

```json
{
  "running": "6.12.0-…",            // 服务器正在运行的内核
  "boot_kernel": "6.12.0-…",        // 下次默认启动的内核
  "needs_reboot": false,            // 服务器自身是否待重启
  "pool": [ {"pkg": "...", "version": "...", "kver": "...", "file": "...deb", "size": 123} ],
  "artifacts": [ {"id": "...", "name": "...", "state": "done", "kver": "...", "debs": ["..."]} ],
  "disk_free_gb": 42.1,
  "lan_ip": "192.168.1.10",         // 客户端接入地址用这个 IP
  "port": 8090
}
```

### GET `/api/logs?n=150` — 服务日志尾部

`n` 上限 1000,默认 200:

```bash
curl -u ":$TOKEN" "http://127.0.0.1:8090/api/logs?n=50"
# → {"lines": ["2026-09-25 10:00:00 INFO ...", ...]}
```

### POST `/api/upload?name=<文件名>` — 上传镜像 / deb

请求体为文件原始字节,**必须有 `Content-Length`**(不支持 chunked):

```bash
# 上传 Armbian 镜像(后台进入流水线,立即返回工件 ID)
curl -u ":$TOKEN" -X POST -T Armbian_skysi-x5.img \
     "http://127.0.0.1:8090/api/upload?name=skysi-x5.img"
# → 202 {"id": "20260925-100000-ab12cd", "state": "processing"}

# 直接上传应用/内核 .deb 入池(自动校验;损坏的包返回 400,不入池)
curl -u ":$TOKEN" -X POST -T myapp_1.2.3_arm64.deb \
     "http://127.0.0.1:8090/api/upload?name=myapp_1.2.3_arm64.deb"
# → 200 {"added": "myapp_1.2.3_arm64.deb", "pkg": "myapp", "version": "1.2.3",
#        "arch": "arm64", "replaced": false}
```

- `.img` / `.tar.gz` / `.tgz`:走完整流水线,磁盘需 `镜像体积 + ~7.5 GB` 空闲,否则 507;
- `.deb`:**应用包直传通道**——先落临时区并用 `dpkg-deb` 读取控制字段校验,通过后原子
  入池并立即重建索引,客户端 `apt update` 后即可 `apt install <包名>` 安装;
  需 `体积 + 2 GB` 空闲;同名文件被覆盖(响应 `replaced: true`);损坏/非标准 deb
  返回 400 与错误信息,不会污染索引;
- `Release` 的 `Architectures` 按池内实际架构动态生成(如 `all arm64`);应用包在
  Web UI 包列表中以「应用」标记,与内核包(显示内核版本)区分;
- 文件名会被规范化为 `[A-Za-z0-9._+-]`。

#### 分块上传(Web 端大文件自动使用)

Web 界面上传超过 16 MB 的镜像时自动分块,无需手工干预:

```
POST /api/upload?sid=<会话ID>&seq=<块序号>&total=<总块数>&size=<文件字节数>&name=<文件名>
```

- 每块 16 MB,服务端逐块追加落盘并记录进度;单块传输中断只回退该块,客户端自动重试
  (连续 4 次失败才中止并给出提示),**不从头重传**;
- 末块完成后进入与整传完全相同的流水线,响应同 `202 {"id": ...}`;
- 分块会话不跨服务重启:重启后续传会得到 `409 会话已失效` 提示重新上传;启动时自动
  清理残留的分块临时文件;
- `curl -T` 的一次性整传接口保持不变,两种方式可混用。

### POST `/api/import` — 导入服务器本地路径

避免大镜像走网络上传:

```bash
curl -u ":$TOKEN" -X POST -H 'Content-Type: application/json' \
     -d '{"path": "/var/tmp/Armbian_skysi-x5_tar.gz"}' \
     http://127.0.0.1:8090/api/import
# → 202 {"id": "...", "state": "processing"}
# .deb 则 → 200 {"added": "...", "pkg": "...", "version": "...", "arch": "..."}
#          (损坏的 .deb → 400)
```

规则:位于 `/tmp`、`/var/tmp` 且不在 `/root` 下的文件**移动**(不占双倍空间),
其余路径一律**复制**——服务承诺绝不移动或删除 `/root/armbain` 工作区内的文件。
支持后缀 `.gz` `.tgz` `.img` `.deb`。

### POST `/api/selfpack` — 自打包当前内核(回滚基线)

把**服务器本机**已安装的内核三件套按同样流程打包入池,用于在升级前留存可回滚版本:

```bash
curl -u ":$TOKEN" -X POST http://127.0.0.1:8090/api/selfpack
# → 202 {"started": true}
```

> 注意:打包的是服务器自身内核。仅当服务器与目标机同板型时才有意义;`x86` 服务器上
> 请在配置中设置 `disable_selfpack: true`(将返回 403)。

### POST `/api/artifacts/<id>/reprocess` — 重新处理工件

工件须保留 `image.img`(即未删除原始解包结果),用于修复问题后重试:

```bash
curl -u ":$TOKEN" -X POST http://127.0.0.1:8090/api/artifacts/<id>/reprocess
# → 202 {"id": "<id>"}    # 工件内已无 image.img 时返回 400,需重新上传
```

### DELETE `/api/artifacts/<id>` — 删除工件

删除该次上传的镜像与元数据(**已入池的 deb 不受影响**):

```bash
curl -u ":$TOKEN" -X DELETE http://127.0.0.1:8090/api/artifacts/<id>
# → 200 {"deleted": "<id>"}
```

### DELETE `/api/pool/<文件名>` — 从仓库删除包

删除 `pool/` 内指定 deb 并自动重建索引:

```bash
curl -u ":$TOKEN" -X DELETE http://127.0.0.1:8090/api/pool/linux-image-...deb
# → 200 {"deleted": "..."}
```

> 危险操作:删除后已升级的客户端无法再从该版本回滚,确认池内仍保留必要的基线版本。

### GET `/apt/**` — 匿名只读的 apt 仓库

供 apt 客户端使用(也可浏览器直接打开查看):

```
http://<IP>:8090/apt/Packages        # 包索引(明文)
http://<IP>:8090/apt/Packages.gz
http://<IP>:8090/apt/Release
http://<IP>:8090/apt/pool/<xxx.deb>  # 下载单个包
```

## 8. 日志

- 标准输出 + `/srv/armbian-iter/logs/service.log`(5 MB 滚动,保留 2 个备份);
- 内容包括:每个 HTTP 请求(来源 IP)、接收上传、重打包、索引重建、工件状态变迁;
- Web UI「服务日志」卡片即 `GET /api/logs`,15 秒自动刷新。

## 9. 常见问题(服务端)

**上传返回 507 "磁盘空间不足"?**
镜像流水线需要 `镜像 + ~8 GB` 临时空间。清理 `artifacts/` 中不再需要 `reprocess`
的旧工件,或换更大磁盘。

**工件报错「镜像缺少本机 dtb」?**
镜像内没有配置 `fdtfile` 对应的设备树。若目标是 Skysi-X5 而服务器不是,在
`/etc/armbian-iter.conf` 中显式设置 `"fdtfile": "rk3588s-skysi-x5.dtb"` 后 reprocess。

**工件报错「镜像内内核数量异常」?**
仅支持单内核镜像。镜像里同时存在多个 `vmlinuz-*` 时拒绝处理,请上传干净的官方镜像。

**工件报错「缺少 xxx.list」?**
镜像内 dpkg 元数据不完整(例如精简过的镜像),无法重打包对应包。

**忘了 token?**
直接编辑 `/etc/armbian-iter.conf` 修改 `token` 字段后重启服务即可。

**Web 上传镜像传到一半失败 / 页面提示网络错误?**
超过 16 MB 的上传自动分块(每块 16 MB),网络闪断时客户端自动重试当前块、不从头
重传;只有连续 4 次失败才中止并提示。整传(curl)或分块中途断开时,服务端会记一条
「上传中断已放弃」日志、自动清理半截文件,不影响服务与其他上传,可直接重试;浏览器
端也会建议改用「服务器本地路径导入」避免大文件走网络。任何未预期错误都会带完整
堆栈记录到服务日志(Web UI「服务日志」卡片可见),不会再无痕迹地断开。

**上传应用 .deb 返回 400"不是标准 .deb"?**
文件损坏或不是 deb 格式(常见:传输中断、传了改名的非 deb 文件)。错误信息中含
`dpkg-deb` 的具体报错;确认文件完整后重传即可。

**池里发现坏包怎么办?**
索引重建时无法读取的 deb 会被自动移到 `/srv/armbian-iter/quarantine/`,不参与索引,
也不会卡死内核流水线;确认无用后可手动清理该目录。

**改端口后客户端 404?**
客户端源里的端口必须同步更新为 `http://<IP>:<新端口>/apt ./`。

**如何彻底重置仓库?**
停止服务后删除 `/srv/armbian-iter/repo`(或仅 `pool/`)再启动,索引会自动重建为空。
