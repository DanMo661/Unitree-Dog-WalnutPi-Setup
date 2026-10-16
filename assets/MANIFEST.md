# 素材清单（大文件不入库，按此获取）

| 素材 | 用途 | 获取方式 |
|---|---|---|
| `V2.4_07-23_server_WalnutPi-1b_6.1.31_ubuntu22.img` | 核桃派 1B 官方系统镜像 | WalnutPi 官网/官方群下载 |
| `walnutpi-1b-netplan.img` | 烙好 netplan（eth0 双地址 123.50+144.10）的部署镜像 | 官方镜像上 loop 挂载写入 `/etc/netplan/01-network-manager-all.yaml` 后重新打包；配置内容见 SOP §0，一次制作反复烧 |
| `wheels2.tar.gz` (~88MB) | 板端 Python 离线依赖（aiortc/fastapi/... 共 52 个 wheel） | 在有外网的机器上按 `requirements.txt` 下载 `pip download -d wheels/`，打包 `tar czf wheels2.tar.gz wheels/`（aarch64 版本） |
| `libjack-jackd2-0_*.deb` + `libportaudio2_*.deb` | sounddevice 运行时库（arm64） | `apt download libjack-jackd2-0 libportaudio2`（arm64 环境）或 Ubuntu ports 仓库 |
| `uwc-fork-pkg.tar.gz` | fork 版 unitree_webrtc_connect（修正 Move 通道/参数） | 从修正版源码打包：`tar czf uwc-fork-pkg.tar.gz unitree_webrtc_connect/` |
| `flash_sd4.ps1` | TF 卡烧录脚本（含 SHA256 全卡校验） | 本仓库 `provisioning/flash_sd4.ps1`；使用前改脚本内 `$img` 路径指向镜像文件 |

> 部署脚本里引用的本地路径（如 `D:\ZcodeRely\...`）按你机器的实际素材存放位置调整。
