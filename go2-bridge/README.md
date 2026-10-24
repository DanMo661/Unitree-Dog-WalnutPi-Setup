# go2-bridge — Go2 SBUS 遥控桥

云卓 G12 遥控器 SBUS → Orange Pi Zero 2W 解码 → WebRTC（MCF 指令）→ 宇树 Go2 Air，
附浏览器遥控页 `http://<板子IP>:8000`（无外部 CDN，断网外场可用）。
接口全部按官方库 legion1581/unitree_webrtc_connect v2.2 核实（含 Move 走 noreply 通道、
SpeedLevel/SwitchAvoidMode 参数为 `{"data": ...}`、StandUp 后补 BalanceStand）。

```
G12遥控器 ──2.4G──► G12接收机 ──SBUS──► [反相] ──► USB-TTL ──► 板子
                                                              ├─ 解码/模式状态机/失控保护
                                                              └─ WebRTC ──► Go2 Air
```

## 硬件清单（板子/TF卡已有）

| 物品 | 说明 |
|---|---|
| SBUS 接入 | **现役方案：40pin 板载串口直入**（10脚 RX2 = `/dev/ttyS2`，config.SBUS_PORT） |
| SBUS 反相 | 反相小板 / MAX3232 模块 / S8050+2×10kΩ 自焊，三选一 |
| 接收机供电 | 先确认型号：R12 类(4.5–5.5V) 从板子 5V 取电；GR01 类(7.2–72V XT30) 需 5V→12V 升压模块 |
| USB-TTL（备选） | 必须支持非标 100000 波特率：CP2102/FT232 稳；接 24pin 扩展板的 USB-A |

> 文档分工：使用=本文件；约定/基线=CLAUDE.md；架构=ARCHITECTURE.md；改造清单=OPTIMIZATION.md；台账=TODO.md。

## 接线

```
G12接收机 VCC ─► 5V(按型号)   GND ─► GND(必须共地)   SBUS ─► 反相器 ─► USB-TTL RX
```

## 固件红线

Go2 固件 **1.1.1~1.1.14 免密钥可用**；在 App 里**关闭"固件自动升级"**锁住 1.1.x。
≥1.1.15 走逐机 AES-128 key（库内 `examples/fetch_aes_key.py` 获取，填 `config.AES_KEY`）。

## 部署（日常改动，Windows 侧一条命令）

```bash
# Git Bash, 在 go2-bridge 目录里(唯一真身; 原版SBUS/历史快照目录只做发行存档):
BRIDGE_PW=<板子密码> python deploy_pc.py               # 上传+md5+板上语法门+重启+验活(烧完自动sync落盘)
BRIDGE_PW=<板子密码> python deploy_pc.py --check-only  # 只读对比, 不动板子
# 板子双路会轮流闪断时指定地址(WiFi): BRIDGE_HOST=192.168.8.52 BRIDGE_PW=<密码> python deploy_pc.py
# 喊话安装页+APK 上架(装在板子 15080):
BRIDGE_USER=pi BRIDGE_PW=<板子密码> python deploy_page.py
```

板子管理地址 `pi@192.168.123.50`（狗网段）；家里 WiFi 为 `.51`(dog2) / `.52`(dog3)。
首次全新装机才需要：scp 全目录 + 板上 `bash install.sh` 装依赖（或跑 `setup3_dog3.py`）。

## 调试五步（每步过了再下一步）

1. **通道实测**：`.venv/bin/python sbus_monitor.py`，摇杆/开关逐个记录通道号和行程，改 `config.py`。
2. **连狗验证**：板子和狗入同一网络（`config.METHOD`），启动后页面/日志出现"WebRTC 已连接"。
   注意手机 App 一次只允许一个客户端，测试时 App 要退掉。
3. **联调**：架空或空旷平地；RC 解锁 → 三段开关试 趴/站/走 → 摇杆（限速 0.5 内）。方向反了改
   `config.py` 通道号取负思路见 CLAUDE.md。
4. **失控测试**：运动中关遥控器 → SBUS 线 0.25s 停走站定（App 网络源 ≈0.55s），30s 真失联上锁保持姿态不趴下（仅 A 键主动上锁/网页退出才趴）。**每次改代码后必测。**
5. **自启**：见下。

## systemd 自启（懒启动设计，无狗无接收机也不影响开机）

```bash
sudo cp go2-bridge.service /etc/systemd/system/
sudo systemctl enable --now go2-bridge
journalctl -u go2-bridge -f        # 看日志
```

## 两种连狗方式（config.py 的 METHOD）

- **LocalSTA**：狗和板子连同一 WiFi/路由器。家里调试推荐，板子保持 192.168.8.51/.52 可 SSH/网页。
- **LocalAP**：板子连狗热点 GO2-XXXXXX（App 里可查密码）。外场推荐，但板子离开家网，远程 SSH 断。

## 15001 控制口行协议（v29+）

- `TRICK <名>`：App 动作面板 → 狗做花样动作。回包 `OK TRICK <名> <秒>` / `ERR LINKDOWN`(狗离线) / `ERR NOTSTAND`(未解锁/非行走/动作占用中) / `ERR BADTRICK`(白名单外) / `ERR NOTREADY`。
  配乐由**板端**联动（config.TRICK_AUDIO，动作成功才播、换动作自动切、对讲中自动跳过）；App 只发 TRICK 一条。
- `BIRD ULTRA|STROBE 0|1`：驱鸟占位协议，回包 `OK BIRD <子> <0|1>` / `ERR BADBIRD`（硬件待接）。
- 动作白名单与时长在 config.TRICKS（时长=占用锁，宁长勿短）；App 面板的显示表须与之一致——**改时长需板端+App 同步升版**。
