# Unitree-Dog-WalnutPi-Setup

**宇树 Go2 机器狗 × 核桃派 ZeroW × G12 遥控器 —— 遥控链路配置流程**

从一张空 TF 卡到"遥控器直控机器狗"的完整配置流程与全套部署源码。
三套实机全流程验证（dog1/2/3），以 dog3 为定稿基准（2026-10）。

---

## 这条链路的本质（不局限于核桃派和 G12）

```
任意 SBUS 遥控器 ──SBUS──► 主控板 ──宇树 WebRTC SDK──► 狗主控 :9991
   │                        │                            ▲
   │ G12 只是其中一种        │ 板内跑"SDK 转发层":        │
   │ (任何能输出 SBUS 的     │  把遥控信号翻译成狗的       │ 狗内各设备
   │  遥控器/接收机都行)     │  WebRTC 运动指令           │ 经网络交换机通信
   ▼                        ▼
GR01 接收机(2.4G 收包)    核桃派只是其中一种
                          (任何能跑 Python 的
                           Linux 板都可以: 树莓派/
                           香橙派/其他国产板)
```

三个角色，各自可替换：

| 角色 | 本项目用的 | 本质 | 可替换成 |
|---|---|---|---|
| 遥控信号源 | 云卓 G12 + GR01 接收机 | 只负责把摇杆/按键变成 **SBUS 信号送进板子**（串口），另外其网口桥走 144 段供 App 使用 | 任何 SBUS 输出的遥控器+接收机（Futaba/富斯等），或去掉物理遥控只用 App 网络遥控 |
| 主控板 | 核桃派 ZeroW（WalnutPi，与 1B 同方案、镜像通用，故文件名带 1b） | 只负责跑**宇树官方 WebRTC SDK 的转发层**（`unitree_webrtc_connect`）：SBUS 解码 → 狗的运动指令（解锁/步态/摇杆/动作），外加调试台/喊话/失控保护 | 任何带串口+网口、能跑 Python 3.10 的 Linux 板；依赖全离线安装（wheels 清单见素材页） |
| 狗内网络 | 成品 5 口交换机 | 板子、狗主控（静态 `192.168.123.161:9991`）、GR01、调试 PC 全部接交换机，一个物理网络两套逻辑段（123/144） | 任何普通交换机 |

> 换板子只需满足：①能从串口读 SBUS（100000bps 8E2，`config.py` 改 `/dev/ttyS*`）②能装 Python 3.10+ 依赖 ③网口接进狗内交换机。换遥控器只需重做**通道标定**（流程第 5 步，每台新遥控器必做）。

---

## 一步一步配置流程（首页直读，每步详细技术坑见 [SOP.md](SOP.md) 对应章节）

### 第 0 步 · 备料
系统镜像（golden 黄金镜像，官方镜像为备用底包）、离线依赖包（52 个 wheel + 2 个 deb）、烧录脚本。全程不需要装额外软件。
全部获取方式与清单：**[assets/MANIFEST.md](assets/MANIFEST.md)**

### 第 1 步 · 烧 TF 卡
读卡器插机箱后面板（TF 卡最低 8GB，推荐 16GB 及以上），管理员 PowerShell：
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File provisioning\flash_sd.ps1 -Image D:\path\to\walnutpi-1b-golden.img
```
默认烧 **golden 黄金镜像**（已烙 netplan 双地址 + SSH + 依赖，上电即可 SSH）。纯 PowerShell + diskpart，**不需要装任何烧录软件**。自动锁定目标卡（拒绝系统盘）→ clean → 写数据区 → 逐字节校验 → **最后写 MBR**。

### 第 2 步 · 板子装机（首板四条命令）
卡进板子、板子进狗（eth0 网线接狗内交换机）、PC 也插交换机并设静态 `192.168.123.99/24`，`ping 192.168.123.50` 通后（PC 需 Python3+paramiko，命令在 Git Bash 执行）：
```bash
cd go2-bridge
python setup4_newdog.py        # 首板一条龙: 代码 + 离线依赖 + venv + 双 systemd 服务 + fork 库 + 音频 deb
BRIDGE_USER=pi BRIDGE_PW=<板子密码> BRIDGE_HOST=<板子IP> python deploy_pc.py     # 写 VERSION + EEE 守卫 + 冒烟
BRIDGE_USER=pi BRIDGE_PW=<板子密码> BRIDGE_HOST=<板子IP> python deploy_page.py   # 遥控器 App 安装/更新页
```
`BRIDGE_HOST` 必须显式写（防裸跑烧错板）；之后滚动更新一律 `deploy_pc.py`。素材路径在脚本顶部，换机器先改。完整 runbook（含烧卡重装后必做清单）：[go2-bridge/docs/NEW-DOG-ONBOARD.md](go2-bridge/docs/NEW-DOG-ONBOARD.md)

### 第 3 步 · WiFi 双档案 = 无线调试根基
**主 WiFi（公司/家里/现场网络）= 静态 IP + 高优先级；手机热点 = DHCP + 低优先级兜底**，切换全自动：
```bash
HOME_PSK=<主WiFi密码> python board_wifi_setup.py \
    --home-ssid "<现场主WiFi名>" --hotspot-ssid "<手机热点名>" --hotspot-psk "<热点密码>" \
    --home-ip 192.168.X.5X    # ★ 每块板不同! 第1块板=.51 第2块=.52 依次递增
```

### 第 4 步 · 狗侧联调（零操作）
狗开机（自检、站立待机）→ 板子自动连狗。验证：
```bash
curl http://192.168.123.50:8000/logs    # 末尾应有 "WebRTC 已连接" + "心跳回读挡位"
curl http://192.168.123.50:8000/sbus    # 浏览器调试台(16通道实时/帧率/链路状态)
```

### 第 5 步 · 遥控器通道标定（每台新遥控器必做）
仓库默认标定是按某一台 G12 实测的（A=ch4、C=ch6、拨杆=ch9，中值 1002、行程 282~1722）。
**新遥控器的通道映射和数值不一样**——症状：继电器有反应但解锁/起立/摇杆全无反应。
标定：调试台 `/sbus` 页逐个拨键看哪个通道在动 → 改 `config.py` 的 `CH_*` 与阈值 → `deploy_pc.py` 重推 →
验收：A 键翻拨出 `已解锁(RC)`、C 拨上狗起立。（详细方法与坑：SOP §5）

### 第 6 步 · 验证清单
解锁/起立/摇杆方向/继电器/调试台帧率/热点入网/主 WiFi 回静态 IP/App 探活喊话动作——
完整勾选清单见 [SOP.md §6](SOP.md)。

---

## 目录

| 路径 | 内容 |
|---|---|
| `SOP.md` | 主文档：每阶段的详细步骤与技术坑（写卡顺序/venv 缺件/SDK 库差异/root 服务/热点上限/驱动僵死/日志分锅…） |
| `go2-bridge/docs/NEW-DOG-ONBOARD.md` | 新狗上门 runbook：分工表、素材位置、EEE 语义、验收清单、烧卡重装后必做清单、实踩坑全集 |
| `provisioning/flash_sd.ps1` | 唯一烧录脚本（先数据区后 MBR，逐字节校验，拒绝系统盘） |
| `provisioning/SD-BURN.md` | TF 卡烧录权威流程：写卡顺序原理、脚本硬性要求、三个脚本坑、验收、废弃方案 |
| `go2-bridge/` | 板端全套源码 + 开发全量归档（bridge.py 转发层 + 部署脚本 + systemd 服务 + 测试 + **android-app 遥控 App 源码** + research 诊断 + tools + sbus_diag + archive + 离线依赖 **wheels2.tar.gz**） |
| `go2-bridge/core/` | 遥控状态机纯逻辑（fsm.py，表驱动可测） |
| `assets/MANIFEST.md` | TF 卡镜像、离线依赖等大文件素材的获取方式 |
| `go2rc/` | G12 SBUS → Go2 Air 遥控桥新手完整指南版（文档级） |
| `go2-bridge-原版SBUS/` | go2-bridge 存档变体（原版 SBUS 接线方案快照） |
| `go2-bridge-板上可用版-20260929/` | 2026-09-29 板上可用快照版 |

> `go2-bridge/` 里与运行/装机直接相关的文件集合以 `tools/make_release.py` 白名单为准；
> 标定用 `calibrate.py` 与 `sbus_monitor.py`；遥控器 App 成品 APK 在安装页/releases，**App 源码已随 2026-11-26 全量归档入库（go2-bridge/android-app/）**。

## 已知边界

- 狗固件锁 1.1.x（1.1.1~1.1.14 免密钥）；≥1.1.15 的狗需逐机取 AES key（`config.AES_KEY`）
- 核桃派 WiFi 芯片（unisoc UWE5622）偶发驱动级僵死，只能断电重启（SOP 坑 3.3）
- 控制状态机双轨：默认 `FSM_MODE="shadow"`（旧路径执行，core/fsm.py 影子只比对不下发）；勿直接改板端配置切 `active`，需先完成影子零差异验证
- 每条狗内网独立可复制部署；**WiFi 侧 IP 必须每板不同**（SOP §3）
