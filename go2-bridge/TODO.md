# TODO / BUG 台账（2026-10-16 整理）

> 代码未动，本文只记账。做完一项勾一项，行号/命令都给好了。

## 🔴 A. 狗网络 20 分钟闪断（头号未破案）

**现象**：开机约 20~30min 后，交换机"狗口"link 灯一下亮一下暗，反复循环；断的窗口里遥控不灵。
**已钉死**：
- 物理层真断（用户实看交换机狗口灯），非应用层
- 板子代码零嫌疑（全部常驻代码已审计：无碰链路层调用、无 20min 定时器）
- 昨日案发时间线（journal 已存 `sbus_diag/20261015-狗网络闪断/`）：
  06:30 WebRTC 连上 → **06:59:56 `ERROR:root:Error processing WebRTC data`（第 30 分钟）** → 07:06 整板异常断电（`FAT not properly unmounted` 实锤）
- 板↔狗与板 WiFi 是两个独立故障（10-15 用户实测：板 WiFi 失联期间遥控全好）

**待办（板子回热点后 3 分钟定案）**：
```bash
journalctl -u go2-bridge --since today | grep -E "心跳|半死|看门狗|链路不通"   # 分锅口诀
# "心跳无回包/判会话半死" → 狗侧官方服务劣化(闭源) → 缓解=预防性定时拆会话 或 UnLeash 进去重启狗服务
# "看门狗: 到狗链路断开" → 真物理断 → 查狗网口/网线/狗内供电欠压(20min≈电池掉压, 交换机成品已排除)
dmesg -T | grep -iE "eth0|link is|phy" | tail -30                            # 内核 link 事件时间戳
ip -s -s link show eth0   # transns 增长=物理层翻转实锤
```

## 🟠 B. 板子 WiFi 三件事（unisoc UWE5622）

1. **真僵死时 nmcli 看门狗无效**（10-14 家里路由器两次实锤）→ 升级方案（驱动重载 `sprdwl_ng` 或僵死超时自动重启）**待用户点头**，风险：重载可能砸内核
2. **板上两个 WiFi 档案补无限重试**（防被热点拒几次后 NM 躺平，今天实锤的坑）：
   `sudo nmcli con modify home connection.autoconnect-retries 0 && sudo nmcli con modify hotspot connection.autoconnect-retries 0` ← 板子回来第一件事
3. **用户手机热点上限=2**（PC+pad 一占满，板子进不来）→ 用户把上限调到 3，或调试时下 pad

**已落地**：wlan0 移交 NetworkManager 双档案（家里=静态 192.168.8.51 优先 / 热点= DHCP 10.243.173.x）、NM 层强制省电关、wifi_watchdog.service（丢包>50%×2 轮自动重连）。部署脚本 `board_wifi_setup.py`。

## 🟡 C. MissionPlanner 安卓版（自修构建进行中）

- 官方安卓 beta **启动即崩**（TypeConverter 缺失，issue #3620，修复 PR #3749 被静默关闭未合并）→ 官方渠道死路
- **自修线已成功**：fork `DanMo661/MissionPlanner` 分支 `fix-android-splash`（commit 9d689cb = PR#3749 补丁打在最新 master），Actions 构建 ✅ run 38781509492，artifact `com.michaeloborne.MissionPlanner.apk.zip`（480MB）
- 待办：下载 artifact（上次被中断）→ adb 装 G12 → 实测 Splash 是否还崩
- **若能跑：把验证结果回帖上游 #3620 / #3749**（对外动作，先过用户）

## 🟡 D. 蹲起动作（方案已备，等用户拍板 4 点）

方案文档 `research/蹲起动作方案.md`。先 B 后 A 节奏 / 蹲起节奏 1.5s×2 / App 按钮"蹲起"位置 / 探针顺序。
- Phase B：BodyHeight(1012) 蹲起，零门禁，MCF 通道直接可用（**含 Pose(1028)≠身高 的勘误**）
- Phase A：SDK2 关节级 500Hz（板装 cyclonedds+sdk2py **需用户点头**；PRO 锁 LowCmd 则 UnLeash-Lite 兜底=动狗系统红线）

## 🔵 E. 小尾巴

- [ ] StandUp 后 pending BalanceStand 不随模式切换清除（狗侧 code=-1 兑底，1 行修，喊了再修）
- [ ] 音频 R 声道恢复方案拍板：①功放输入并联 L（零成本） ②USB 声卡 CM108（兼麦克风，推荐）
- [ ] PC 网卡 tx hang 治本：关 EEE / Gigabit Lite / 环保节能（改系统设置，**需用户批准**）
- [ ] 导航接线后 axis() 标定复核（飞控输出的 SBUS 中值/行程 vs G12 标定值 1002/282~1722，`calibrate.py`）

## ✅ 2026-10-13~16 已完成

- pwm1/15脚舵机删除（ch10 让给拨杆）+ 趴下解锁弹起修复（C 键边沿触发）+ 6 测全绿，d882714 已部署 dog2
- KGG 音乐解锁管线（kgg_unlock.py + push_song.py，酷狗加密→PCM→板端喊话库）
- Go2 低层控制调研留底（`research/go2_lowlevel_调研留底.md`，SDK2/ReleaseMode/UnLeash/MCF 指令表）
- 板子 WiFi 双档案 + 看门狗 + 热点调试通道打通（10.243.173.31）
- G12 三地面站：LGroundControl(CUAV) ✅ / QGroundControl v5.1.5 ✅ / MissionPlanner 自修版构建中
- G12 装 AppManager → **以后遥控器自助装 APK 免 adb**（系统"点 APK"链路是断的，AppManager 接上了）
