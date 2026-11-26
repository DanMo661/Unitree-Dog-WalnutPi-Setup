# TODO / 阻塞台账（2026-10-21 修订）

> 本文件只放**未完成/阻塞项**。完成项进 git log 与归档，不再堆积。
> 架构级改造计划见 OPTIMIZATION.md；项目约定见 CLAUDE.md。

## 🔴 A0. 开机首个起立弹起 → 狗对外供电口断电（10-20，dog2）

**现象**：上电后第一次"解锁→趴下→起立"，狗抽搐一下、上装板整体掉电重启（狗本体有电）。
此后趴下/起立/动作全部正常；用户手动"多按几下解锁、先按别的键再起立"可绕开。
黑匣子最后一行是写了一半的 JSON（断电前约 2 秒的 cmd/arm/mode 还在页缓存）。

**已定位（10-21 黑匣子更正）**：10-21 10:57 复现时**全程没有 BalanceStand**；断电前序列是
`StandUp` → 14ms 后 `EconomicGait`（bridge/板子重启后 `gear_sent=0`，起立帧同时补挡位）→ 上装失电。
根因是**起立流程中并发的步态指令**；旧 3s BalanceStand 是另一条历史风险路径（已被状态驱动+低位不盲发修掉）。
- 修复：`rc_loop`/`core/fsm` 的步态同步加 `not rising`（起立完成后的下一帧才发），commit `3fec1f1`。
- 10-21 11:18~11:20 真机三次起立：`StandUp` → 2.0s stable → `BalanceStand`，无步态并发、上装未断电。

**已做（10-20 代码提交）**
- 解锁不再自动趴下；趴下只认 C 下降沿。
- 趴/起立大动作 2 秒互锁；A 键解锁去抖；开机 5 秒起立保护期。
- 订阅狗真实姿态 `rt/sportmodestate` + `rt/lf/sportmodestate`；首连按 `body_height` 定 mode。
- `StandUp` 后等 `body_height` 0.6s 稳定再 `BalanceStand`（无数据回落 3 秒，6 秒兜底）；起立中屏蔽 Move。
- 黑匣子关键事件 fsync + `.running/.clean` 停机标记；`/state` 加 `dog/pids/last_cmd/last_events/sys/last_stop`；
  新增 `/journal?n=`；调试台加"系统与事件 + 狗真实姿态"。

**待办**
- [x] 真机复现开机首起立：10-21 三次起立全过（`stand_up` → stable 2.0s → `BalanceStand`，上装未断电）。
- [ ] 若以后在**没有起立动作**时仍触发对外口断电 → 硬件方案定案（按顺序试）：
      1. 上装板改独立供电（5V 充电宝，狗 12V 只带飞控/接收机）；
      2. 12V 输入加 12V 小 UPS/储能模块 + 1000µF 低 ESR + TVS；
      3. 飞控/接收机改独立 BEC，切断与板子的地环。
- [x] 操作习惯变更告知：解锁后狗不再自动趴下，趴/站只认 C 键翻拨（已告知）。

## ✅ A. 狗网络 ~20-30 分钟闪断（已破案：EEE，2026-10-21）

**现象**：交换机"狗口"link 灯周期闪烁，断窗内遥控不灵；用户实看为物理层。
**已排除**：板子代码零链路层调用；板 WiFi 故障与狗口闪断是两起独立故障（10-15 实测）。
**手上证据**：10-15 时间线 `sbus_diag/20261015-狗网络闪断/`：06:30 连上 → 06:59:56
`Error processing WebRTC data`（第 30 分钟）→ 07:06 整板异常断电（FAT not unmounted）。

**10-21 定案**：狗 RTL8168 与 10/100 交换机的 EEE 协商导致每 5~15s 链路重协商（Down 恰好 3s），
表现为灯闪/丢包/桥每分钟重连。关闭 EEE 后 600 ping 0 丢包、16 分钟零 flap；持久化已装
`/etc/network/if-up.d/eee-off`（可逆，删文件即回退）。详见 `research/go2_network_diagnosis.md` §I。

**历史诊断命令留档（已定案，不再需要）**
```bash
journalctl -u go2-bridge --since today | grep -E "心跳|半死|看门狗|链路不通"
# 心跳无回包/半死 → 狗侧服务劣化（缓解=预防性拆会话或 UnLeash 重启狗服务）
# 看门狗: 到狗链路断开 → 物理断 → 查狗网口/网线/狗内供电（20min≈电池掉压）
dmesg -T | grep -iE "eth0|link is|phy" | tail -30
ip -s -s link show eth0   # transns 增长=物理层翻转实锤
```

## 🟠 B. 板 WiFi（unisoc UWE5622）

- [x] 2026-10-21 修 `wifi_watchdog.sh` 的 BusyBox ping 参数：原 `ping -I wlan0 ...` 在板上非法，
      每轮判 100% 丢包 → 每 2 分钟 `nmcli disconnect/connect wlan0`（桥随之拆会话）。
      已改为 `ping -c 4 -W 2 -q $gw`（备份 `.bak_20261021`），桥看门狗加 5s 载波宽限（commit 9255f7e）。
- [ ] dog2 板补 `autoconnect-retries 0`（dog3 已由 setup3 落地）：
      `sudo nmcli con modify home connection.autoconnect-retries 0`
      `sudo nmcli con modify hotspot connection.autoconnect-retries 0`
- [ ] 驱动真僵死时 nmcli 无效 → 驱动重载方案（`sprdwl_ng`）**待用户点头**（有砸内核风险）
- [ ] 用户热点连接上限=2，调试时 PC+pad 占满板子进不来 → 调到 3 或下 pad

## 🟡 C. dog2 板代码落后（部署前必须决策）

dog2 板跑 `d882714`（10-14），dog3 跑 `e4de755`（10-17）。直接推新代码会同时改变两件事：

1. **继电器键位**：D 键(ch8)→pwm2 **变成** B 键(ch5)→pwm1（dog2 实物接线是否已改？）
2. **音频声道**：L-only **变成** 双声道（dog2 的 R 输出级硬件损坏）

→ 决策：要么 dog2 保持旧代码，要么先做 P1-3（`config_local.py` 板级覆盖）再同步。

## 🟡 D. dog3 继电器失效（10-17，用户判定硬件问题）

**已确认**：代码路径正确（`bridge.py:559-562` 只在干净 SBUS 帧时跟随 B 键）；pwm1
sysfs 处于 `enable=1 / period=20ms / duty=1050us`（断开位），服务启动时初始化成功。
**未完成**：板子当时双路失联，未能确认 SBUS 是否在进帧（`/state` 缺失，见 OPTIMIZATION P0-2）。
**下一步**：板子回来后读 `/state` 的 `sbus.ok/fps/ch[5]` + `relay`；若 SBUS 正常仍不吸合 → 查继电器模块供电/接线。

## 🟡 E. 蹲起动作（方案已备，等拍板 4 点）

`research/蹲起动作方案.md`：先 B 后 A 节奏 / 蹲起节奏 1.5s×2 / App 按钮位置 / 探针顺序。
- Phase B：BodyHeight(1012) 零门禁；Phase A：SDK2 关节级需装 cyclonedds+sdk2py（待点头）。

## 🟡 F. 音频遗留

- [ ] 快速连点 PLAY 时旧 aplay 未退干净 → `Device or resource busy`，第二次播放静默失败（10-17 13:48 实锤）。
      随 P0-1 拆分时给音频服务加"启动失败重试 1 次"。
- [ ] 10-17 13:50 主进程非正常退出（journal 无 Stopping 记录，systemd 拉起）。
      traceback 未取到（板子随后离线）。**这是 P0-1 拆进程的直接动机**。
- [ ] dog2 音频策略确认：R 声道损坏，需 `PA_STEREO=False`（P1-3 的 `config_local.py`）。

## 🔵 G. 其它小尾巴

- [ ] PC 有线网卡 tx hang 治本：关 EEE / Gigabit Lite（改系统设置，需批准）
- [x] 公开仓 `Unitree-Dog-WalnutPi-Setup` 快照同步（10-21 阶段3 重跑落地，53 文件 md5 全对，
      VERSION=d967cce-phase3，**未 commit 未 push**）→ push 前过目公开仓文档 4 条更新项
      （README 补 core/fsm.py 行与 FSM_MODE 提醒 / SOP §5 标定指引改指 config.py / 详见阶段报告）
- [ ] App 目标 IP 写死 `192.168.144.10`（`PaService.java:74`）→ P2-1
- [ ] 板子双路（eth0/WiFi）10-17 多次失联 → P2-2 留证；硬件侧继续观察

## 🟣 H. 拆分/删旧路径收尾（2026-10-31 深夜，随 bridge.py 拆分牵出）

旧控制路径已删（`f80be4f`，合并 `9a6deae`，ADR-0004 后续记录；回退锚点 tag `pre-split-20261031`）。
收尾项：

- [ ] **删后首道真机门**：`docs/REGRESSION-CHECKLIST.md` 12 场景全跑（ADR-0004 后续记录约定次日执行）。
- [ ] **`config.FSM_MODE`/`FSM_SHADOW`/`_DENY_KEYS` 下版本移除**：暂留只为防板上旧 config_local
      报 AttributeError，已无生产读取方（CLAUDE.md 配置节 / ARCHITECTURE.md §4 同口径）。
- [ ] **`_deploy_dog2` 变体去留**：待用户拍板——收敛为只留 `config_local.py` 差异
      （`PA_STEREO`）或整体清理（REFACTORING-PLAN 1.3 / ARCHITECTURE.md §9）。
- [ ] **ADR-0001 写者登记遗留两项**（ARCHITECTURE.md §6 状态分布表"待登记"）：
      ① `state.web_armed` 双写点（webserver WS 线程 + `FsmActive._sync`）收敛为单写点；
      ② `state.last_cmd` 跨线程 dict（submit 调用线程建、DogLink 线程改 result）登记定案。

## ✅ 已完成（2026-10-13 ~ 10-17，摘要）

- 继电器 v2（B 键 ch5 → PWM1/15脚两档 1950/1050us + 4 路 GPIO），dog3 实测通过
- 危险 bug：模式切换清空 pending（起立 3s 内拨 C 不再弹起），红绿验证 + 六测全绿
- dog3 装机：烧卡 PASS + setup3 全套 + WiFi 双档案（.52）+ 新遥控器全家桶
  （PaSend v35 / MP 修复版 / QGC / LGC / AppManager）
- dog3 音频破案：根因=功放接 R、代码只写 L → 双声道同数据修复，7Years 实测出声
- MissionPlanner 安卓自修：fork + PR#3749 补丁构建 + 本地签名，实机可用；上游回帖已发
- 全流程开源 `Unitree-Dog-WalnutPi-Setup`（SOP 自包含化 + nitpick 两轮 30 条闭环）
- GitHub 生态整理：三狗仓库统一 topic + profile「宇树机器狗专区」
