# 客户端文档

本文档面向局域网内通过 apt 从 `armbian-iter` 仓库升级内核的设备(目标机)。
只要是与镜像匹配的同板型 arm64 Debian/Armbian 设备即可,无需安装任何额外软件。

服务端的部署与 API 请看 [server.md](server.md)。

## 1. 接入仓库

假设服务端 IP 为 `192.168.1.10`(以 Web UI「客户端接入」卡片或服务端 `/api/status`
的 `lan_ip` 为准),端口默认 `8090`:

```bash
echo 'deb [trusted=yes] http://192.168.1.10:8090/apt ./' | sudo tee /etc/apt/sources.list.d/armbian-iter.list
sudo apt update
```

`apt update` 无报错即接入成功。也可在浏览器打开 `http://192.168.1.10:8090/apt/Packages`
确认索引内容。

## 2. 升级内核

```bash
apt list --upgradable                        # 查看可升级项(内核三件套)
apt policy linux-image-edge-rockchip64       # 查看仓库提供的所有版本
sudo apt upgrade                             # 或只升级三件套:
sudo apt install linux-image-edge-rockchip64 \
                 linux-dtb-edge-rockchip64 \
                 linux-headers-edge-rockchip64
```

仓库提供的内核三件套:

| 包 | 内容 |
| --- | --- |
| `linux-image-edge-rockchip64` | 内核镜像 `/boot/vmlinuz-*` 与模块 |
| `linux-dtb-edge-rockchip64` | 设备树 `/boot/dtb-*` |
| `linux-headers-edge-rockchip64` | 内核头文件(编译模块用) |

版本号规则:`镜像内原版本 + <内核版本>`,例如 `26.11.0-trunk+7.2.4`。
**三个包应保持同一版本**,一起升级/回退。

### 重启生效

内核升级不会热生效。服务端也**绝不会**远程重启你的机器——请在方便的时机自行重启:

```bash
sudo reboot
uname -r        # 重启后确认已运行新版本
```

升级后、重启前,`apt list --upgradable` 不再显示三件套,当前运行内核仍是旧的,属正常现象。

## 3. 回滚到旧版本

先查仓库里有哪些历史版本:

```bash
apt policy linux-image-edge-rockchip64
```

三件套一起回退(版本号以 `apt policy` 实际输出为准):

```bash
sudo apt install \
  linux-image-edge-rockchip64=26.11.0-trunk+7.2.4 \
  linux-dtb-edge-rockchip64=26.11.0-trunk+7.2.4 \
  linux-headers-edge-rockchip64=26.11.0-trunk+7.2.4 \
  --allow-downgrades
sudo reboot
```

如需在该版本停留,防止后续 `apt upgrade` 又升上去:

```bash
sudo apt-mark hold linux-image-edge-rockchip64 linux-dtb-edge-rockchip64 linux-headers-edge-rockchip64
# 解除:sudo apt-mark unhold <同上三个包>
```

> 注意:只能回退到**仓库池中仍存在**的版本。请勿在服务端随意删除旧版本 deb
> (见 [server.md](server.md) 中 `DELETE /api/pool/` 的警告)。

## 4. 不接入源、直接下载安装

一次性安装某几个包,不配置 apt 源:

```bash
# 浏览器打开 http://192.168.1.10:8090/apt/Packages 查看池内文件名
curl -O http://192.168.1.10:8090/apt/pool/linux-image-edge-rockchip64_26.11.0-trunk+7.2.4_arm64.deb
sudo apt install ./linux-image-edge-rockchip64_*.deb
```

## 5. 移除仓库

```bash
sudo rm /etc/apt/sources.list.d/armbian-iter.list
sudo apt update
```

已安装的内核包不受影响;如需连同内核一起卸载,用 `apt remove` 处理对应三件套。

## 6. 常见问题(客户端)

**`apt update` 报 404 / 无法连接?**
确认服务端 IP、端口与服务进程;源地址必须以 `/apt ./` 结尾(注意中间有空格)。
服务端改过端口的话,源里的端口要同步改。

**`trusted=yes` 安全吗?**
它表示信任这个未签名的仓库。本服务面向局域网,`/apt/*` 只读且仅含你上传的包,
风险可控。仓库未做 GPG 签名,因此该选项是必需的;若对供应链有更高要求,
可在服务端下载 deb 自行签名后另行分发。

**升级后没有出现新版本?**
服务端上传的镜像可能仍在处理中(Web UI 可见状态),或处理失败;完成后客户端
`apt update` 即可看到。

**升级后设备无法启动?**
用串口/救砖介质启动后,通过另一系统挂载本机磁盘,或在恢复环境中把
`/boot` 指向的内核链接回退,再进入系统执行 §3 回滚。升级前在服务端点
「自打包当前内核」留存基线,可让回滚始终有包可装。

**`/etc/apt/sources.list.d/` 下多个源都提供同名包?**
`apt policy` 可看每个版本的来源;必要时用 `Pin` 或删除多余源,确保三件套来自本仓库。
