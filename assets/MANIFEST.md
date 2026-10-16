# 素材清单与自制方法（大文件不入库，按此获取/制备）

> 与 SOP 的对应关系：§1 烧卡素材 / §2 部署素材 / §3 WiFi 无需素材 / §4 固件工具

## §1 官方系统镜像

核桃派 1B 官方发布（官网 walnutpi.com 或官方群）：`V2.4_07-23_server_WalnutPi-1b_6.1.31_ubuntu22.img`（server 版 Ubuntu 22.04，内核 6.1.31）。
用其他版本镜像原则上也可，但以下路径/依赖均以该版本实测。

## §2 netplan 部署镜像自制（烙 eth0 双地址，一次制作反复烧）

官方镜像上把网络配置写死进去，这样板子一上电就有 123.50 + 144.10，不用接串口配网。
在 WSL（或任何 Linux）里：

```bash
# 1. 把官方镜像挂成回环设备（记住输出里的 /dev/loopX）
sudo losetup -Pf --show WalnutPi-1b官方.img     # 输出例如 /dev/loop0

# 2. 挂载根分区(第2分区)并写入 netplan 配置
sudo mkdir -p /mnt/img
lsblk -f /dev/loop0        # 先确认分区布局: ext4 的根分区通常是 p2
sudo mount /dev/loop0p2 /mnt/img
sudo tee /mnt/img/etc/netplan/01-network-manager-all.yaml << 'EOF'
network:
  version: 2
  ethernets:
    eth0:
      dhcp4: false
      addresses:
        - 192.168.123.50/24
        - 192.168.144.10/24
EOF

# 3. 卸载并保存为部署镜像(改个名区分)
sudo umount /mnt/img
sudo losetup -d /dev/loop0
mv WalnutPi-1b官方.img walnutpi-1b-netplan.img
```

> 只烙 eth0，**不烙 WiFi**（SOP 坑 1.1：WiFi 交给 NetworkManager 双档案）。

## §3 离线依赖 wheels（52 个，~88MB）

板子部署环境可能无外网，全部依赖离线灌入。在有外网的任意机器（Windows/Linux 均可）下载 arm64 版：

```bash
# 版本锁: unitree_webrtc_connect 必须 ==2.0.0 (2.2.0 引入 pyaudio/opencv 等
# 无 aarch64 wheel 的依赖, 下载直接失败; 板上实际用本页 §5 fork 包覆盖, 版本只影响依赖解析)
pip download \
    -d wheels/ \
    --platform manylinux2014_aarch64 --platform any \
    --python-version 310 --only-binary=:all: \
    unitree_webrtc_connect==2.0.0 pyserial fastapi uvicorn
tar czf wheels2.tar.gz wheels/
```

（`requirements.txt` 在仓库 `go2-bridge/` 下；`unitree_webrtc_connect` 这一项 PyPI 原版有缺陷，会被 §5 的 fork 包覆盖，照下无妨。）

## §4 系统 deb（2 个，sounddevice 运行时库）

arm64 版，在 arm64 Linux 环境或 Ubuntu ports 仓库获取：

```bash
apt download libjack-jackd2-0:arm64 libportaudio2:arm64
```

地址形式：`http://ports.ubuntu.com/ubuntu-ports/pool/main/...`（按包名搜）。
缺这两个 deb 的症状：`setup2_extras.py` 的 `FORK_OK` 自检报 `PortAudio library not found`（SOP 坑 2.2）。

## §5 fork 版 SDK 库打包（连狗引擎，覆盖 PyPI 原版）

```bash
git clone https://github.com/DanMo661/unitree_webrtc_connect.git
cd unitree_webrtc_connect
tar czf ../uwc-fork-pkg.tar.gz unitree_webrtc_connect/
```

fork 相对 PyPI 原版的修正：Move 指令走 noreply 通道、SpeedLevel/SwitchAvoidMode 参数名、
Windows 中文环境编码崩溃、大文件分块传输（对应上游 legion1581 PR #63/#64）。
`setup2_extras.py` 会把它解压覆盖进板子 venv 的 site-packages。

## §6 固件 ≥1.1.15 的 AES key 获取工具

狗固件 ≥1.1.15 时 WebRTC 信令加密，需逐机取 key 填 `config.AES_KEY`：
上游库自带 `fetch_aes_key.py`（本仓库 §5 的 fork 里也有），在能通狗 `:9991` 的机器上按其 README 运行即可。
固件 1.1.1~1.1.14 无此步骤（SOP §4 已说明）。

## §7 遥控器 App 安装包（PaSend.apk / GO2控制台）

App 源码在仓库 `go2-bridge/android-app/`（单 Activity Java，无第三方依赖）。
成品 APK 不入 git（.gitignore 排除二进制），两个来路：
- 部署时用环境变量指定本地成品：`BRIDGE_APK=<APK路径> python deploy_page.py`（缺省找脚本同目录 `PaSend-v35-网络遥控.apk`）
- 或自行构建：任意 Android 工程套入该 Java 源码 + `AndroidManifest.xml`（权限/组件声明都在），`aapt+d8+apksigner` 或 Android Studio 皆可

## §8 烧录脚本

`provisioning/flash_sd4.ps1`（本仓库已带）。使用前改脚本开头的 `$img = ...` 一行：
```powershell
$img = 'D:\你的路径\walnutpi-1b-netplan.img'
```
