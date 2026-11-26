# go2-bridge 架构说明（现状 + 目标）

> 本文描述**代码怎么组织、数据怎么流、状态在哪里**。改造计划见 OPTIMIZATION.md 与
> REFACTORING-PLAN.md，项目约定/红线见 CLAUDE.md。
> 行号对应 2026-10-31 深夜的 `b2fd0c5`（bridge.py 拆分 + 旧控制路径删除落地后；
> 当日早间快照另见 ARCHITECTURE-20261031.md）。

---

## 1. 系统拓扑

```
G12 遥控器 ──2.4G──► 接收机 ──SBUS(100Hz, 反相)──► 板 40pin 10脚(ttyS2)
                                                      │
G12 App「手动控制」──UDP:15010──────────────────────►│  (300ms 新鲜时优先于 SBUS)
                                                      ▼
                                              ┌───────────────┐
浏览器 8000/ws ──────────────────────────────►│  go2-bridge   │
G12 App 喊话/动作 ──UDP:15000/15001/15003───►│  (go2-audio)  │
                                              └───────┬───────┘
                                    动作转发 UDP:15011 │ WebRTC 信令 9991 + MCF 指令
                                                      ▼
                                                  Go2 狗 (192.168.123.161)
```

硬件三角色（与具体硬件无关）：**遥控信号源**（SBUS 线或 UDP 网络遥控）→ **SDK 转发层**（板子）→ **被控对象**（狗）。
板子与狗同一交换机/同一狗内网；主测板哨兵S1 家 WiFi `192.168.8.53`，狗侧管理地址 `192.168.123.50`（eth0）+ `192.168.144.10` 别名，App 侧走别名。

## 2. 进程与线程模型（现状）

两个 systemd 服务：`go2-bridge`（控制，root，`Restart=always`，`RestartSec=5`）与
`go2-audio`（音频独立进程，跑 pa_recv.py，P0-1 已落地）。桥进程 5 个线程：

| 线程 | 入口 | 职责 | 故障后果 |
|---|---|---|---|
| 主线程 | `rc_loop()` → `_rc_loop_active()` bridge.py:361/318 | 20Hz 读 SBUS/网络遥控 → 喂 DogFsm → 落地输出 | **异常=进程死** |
| WebRTC | `DogLink._run()` link.py:166 | asyncio：连接/看门狗/心跳/指令队列/狗状态订阅回调 | 只影响连狗，自动重连 |
| 网页 | `web_server()` webserver.py:88 | uvicorn：`/`、`/sbus`、`/state`、`/logs`、`/journal`、`/ws` | daemon 线程 |
| 网络遥控 | `netrc._loop()` netrc.py:36 | UDP:15010 杆量接收 | daemon 线程 |
| TRICK 本地口 | `trick_udp_loop()` bridge.py:366 | UDP:15011 收 go2-audio 转发的动作请求 | daemon 线程 |

音频的 3 个工作线程（15000 对讲 / 15001 控制 / 15003 上传）在 go2-audio 进程内，
经 HTTP 只读桥 `/state`、UDP:15011 转发动作，与遥控互不拖累。

## 3. 模块清单（2026-10-31 拆分后）

bridge.py 2473 行 → **454 行**，拆出四个平铺模块（每步行为零变更：
9747bbf/227734a/ca2dcd6/4e179da 四步提交）：

| 文件 | 行数 | 一句话职责 |
|---|---|---|
| bridge.py | 454 | 装配+调度：`FsmActive`（FSM 执行器，唯一控制器）、`_rc_loop_active` 主循环、`rc_loop` 薄委托、TRICK UDP 口、`main` |
| link.py | 647 | `DogLink` WebRTC 会话线程（永远重连/看门狗/心跳/指令队列）+ unitree 库导入 + 狗地址解析（`_pick_robot_ip`/`_scan_9991`）+ 狗状态订阅回调 |
| webserver.py | 300 | `web_server` + FastAPI 路由 + WS 指令通道（arm/axes，缺键不清零） |
| telemetry.py | 125 | `State`+`state` 单例、`log`/`_LOG_BUF`、`_ev`/`_EVENTS`、`_dog2_snapshot`/`_dog_height_valid`/`clamp01` 共享设施 |
| stats.py | 93 | `SbusStats` + `sbus_stats` 单例（60s 秒桶帧统计/丢包/告警） |
| core/fsm.py | 664 | `DogFsm` 纯逻辑状态机（唯一指令源，表驱动可测 42 例） |
| config.py | 192 | 全部可调参数（`FSM_MODE`/`FSM_SHADOW`/`_DENY_KEYS` 暂留无读取方，见 §4） |
| netrc.py / sbus.py / languard.py / journal.py | 84/112/77/322 | 输入前端与设施（languard 纯 stdlib；journal 异步黑匣子） |

**依赖边界**：`core/` 自包含（唯一豁免 config）+ `languard.py` 纯 stdlib，
`tools/check_deps.py` 守门（`test_deps.py` 固化）；发布集校验 `test_release_files.py`。
**循环依赖处理**：webserver/link 对 bridge 用惰性 import（`_fsm_feed` 读 `bridge._ACTIVE`、
WS 推送读 `bridge._ctl_snapshot`）；`FsmActive.__init__` 惰性 import `core.fsm`
（core/ 未部署则启动即失败，部署清单必须带 core/）。

## 4. 数据流

**控制主链（现役唯一）**：`Sbus.read()` → `netrc.get()` 覆盖（300ms 新鲜优先）
→ `_rc_loop_active` 每帧喂 `DogFsm`（core/fsm.py：解码/边沿/互锁/起立流程/挡位/失控保护）
→ 唯一执行句柄 `FsmActive`（构造时注入 `send=link.submit`，FSM 在事件内同步真发）
→ 指令队列（Move 只留最新）→ `_send()` → WebRTC `SPORT_MOD`。

**旧控制路径已删（2026-10-31 深夜，ADR-0004 后续记录）**：`Dog`/`FsmShadow`/`_RecLink`/
`_RecRelay`/`_collapse`/`_shadow_safe`/`_SHADOW`/`_fsm_mode`/旧 rc_loop 体全部移除
（f80be4f，随拆分分支合并 9a6deae）；`rc_loop` 现为薄委托 → `_rc_loop_active`（bridge.py:361）。
`FsmActive` 是唯一控制器。`config.FSM_MODE`/`FSM_SHADOW`/`_DENY_KEYS` 暂留无生产读取方
（防板上旧 config_local 报 AttributeError，`_DENY_KEYS` 仍拦 FSM_MODE 进板级差异），
下版本移除（TODO H）。回退锚点：tag `pre-split-20261031`。

**checked-submit**（契约全文见 `core/fsm.py` 头注释）：`set_mode` 的 StandDown/StandUp/BalanceStand、
`_finish_rise`、`trick`、`move`、`hold` 入队失败不提交本地状态（mode/rising/moving/冷却），
请求标志留给下一帧重试 —— StandUp 没发出去绝不进入 rising，之后绝不向趴着的狗补 BalanceStand。

**指令通道**：`Move` 走 noreply 流（20Hz）；其余走 `publish_request_new` 等回包，10s 超时（`REQ_TIMEOUT_S`）。
**心跳**：每 5s `GetSpeedLevel` 探活，连续 2 次无回包判"会话半死"→ 拆会话重连（link.py:295-326）。
**看门狗**：每秒读 eth0 carrier、每 3s 探狗 9991 口；断则强拆重连（link.py:257-292）。

**音频链**（go2-audio 独立进程）：App UDP:15000 PCM → `pa_recv` 软件增益 → `audioop.tostereo` → `aplay -c 2`；
预设播放读 `/root/pa-files/<名>.pcm`；上传走 15003 分块+CRC+fsync。
**动作联动**：App `TRICK` → 15001 → pa_recv → 桥 UDP:15011（`trick_udp_loop`，仅回环）→
`FsmActive.trick` → 回包 OK/LINKDOWN/NOTSTAND 原样透传；App "RC" 轮询由 pa_recv HTTP 读
桥 `/state`（契约见 ADR-0007）。

**网页链**：WS 会话 axes → `state.web_v*` → FSM `tick` 网页驱动（仅 RC 上锁时）；
`cmd` → `FsmActive.web_cmd`/`set_mode`；关网页 finally → `hold()`（解除接管+停走保持姿态，永不趴，ADR-0003）。

## 5. 协议一览

| 端口 | 方向 | 协议 | 用途 |
|---|---|---|---|
| 9991 | 板→狗 | WebRTC 信令 | con_notify 建会话 |
| 8000 | 浏览器→板 | HTTP + WebSocket | 遥控页 / 调试台 / 日志 |
| 15000 | App→板 | UDP 裸 PCM | 实时喊话音频（go2-audio） |
| 15001 | App→板 | UDP 行协议 | 喊话控制/预设/动作/RC 查询（go2-audio） |
| 15003 | App→板 | UDP 二进制分块 | 预设音频上传（CRC+累计确认，go2-audio） |
| 15010 | App→板 | UDP JSON | 网络遥控杆量（替代 SBUS 线） |
| 15011 | go2-audio→桥 | UDP 行协议 | TRICK 动作转发（仅回环 127.0.0.1） |
| 15080 | App→板 | HTTP | deploy_page 版本.json + APK 上架（App 更新通道） |
| `rt/lf/sportmodestate` | 狗→板 | WebRTC 订阅 | **狗真实姿态**：mode/body_height/gait/velocity/stamp（10-20 起用于控制与调试台） |
| `rt/sportmodestate` | 狗→板 | WebRTC 订阅 | 高频姿态（部分固件不发；两路都订，谁到用谁） |
| `rt/lf/lowstate` | 狗→板 | WebRTC 订阅 | 关节 q/温度（20 槽前 12 实装）+ bms_state + 机身温度——GO2 信息卡数据源（10-31） |
| `rt/lf/mainboardstate` | 狗→板 | WebRTC 订阅 | 机身温度 temperature_ntc1（本固件实测不发，保留订阅） |

另有**探测订阅**（BMS_STATE/SELF_TEST/SERVICE_STATE/LIDAR_STATE/SECONDARY_IMU/
WIRELESS_CONTROLLER/UWB_STATE，10-31）：每话题进程内只记一次首包 debug，确认固件
到底发什么；`/state` 的 `dog`（姿态）与 `dog2`（关节温度/电池/姿态角/新鲜度）段即来自以上订阅。

SBUS：100000bps 8E2，25 字节/帧，16 通道 × 11bit；行程 172~1811、中值 992（接收机侧），
映射后中值 1002 → `axis()` 归一化。通道映射与阈值见 `config.py:44-85`。

## 6. 状态都在哪里（现状 + 唯一写者登记）

> ADR-0001：共享 state 不上锁，每字段**唯一写者**，跨线程只允许单属性原子赋值，
> 读-改-写复合操作只许发生在唯一写者线程内。新增 state 字段必须在本表登记写者。

| 位置 | 内容 | 唯一写者（线程 / 写点，行号实测 b2fd0c5） |
|---|---|---|
| `State` 单例（telemetry.py:36-91） | `link`（property→`_link`） | DogLink 循环线程：link.py:168/187/192/199/211/226（`_run`/`_session`） |
| | `link_gen`（`+=`） | DogLink 循环线程：link.py:212（读改写只在写者线程内，符合 ADR-0001） |
| | `mode`/`rc_armed`/`gear`/`action`/`action_end`/`sbus_ok`/`sbus_clean`/`arm_blocked_until`/`web_armed` | 唯一写点 `FsmActive._sync`（bridge.py:162-186）——入口跨 rc 主循环/web/DogLink 三线程，恒在 `FsmActive.lock` 内串行，单属性赋值 |
| | `web_vx`/`web_vy`/`web_vyaw` | uvicorn WS 会话线程：webserver.py:243-247（断开清零 :293） |
| | `last_src`/`last_frame_t` | rc 主循环线程：bridge.py:343/348 |
| | `relay_us`/`relay_on` | 唯一写点 `FsmActive._exec`（bridge.py:140-141/150，`FsmActive.lock` 内） |
| | `last_big_sent` | DogLink 循环线程：link.py:549/584（`_consume`/`_nonmove_worker`） |
| | `loop_ts` | DogLink 循环线程：link.py:331（`_loop_ticker`） |
| | `dog_mode`/`dog_height`/`dog_gait`/`dog_vel`/`dog_state_t`/`dog_state_n`/`dog_rpy`/`dog_imu_t`/`dog_err` | DogLink 循环线程：link.py:366-391（`_on_sport_state`） |
| | `dog_joints`/`dog_bms`/`dog_low_t`/`dog_low_n` | DogLink 循环线程：link.py:405-444（`_on_low_state`） |
| | `dog_mb`/`dog_mb_n` | DogLink 循环线程：link.py:449-461（`_on_mb_state`） |
| | `last_cmd` | **待登记**：dict 创建在 submit 调用线程（link.py:636，经 FSM send= 由 rc/web/DogLink 任一线程触发）；`result` 原地改在 DogLink 循环线程（telemetry.py:94-98 `_set_cmd_result` ← link.py:515/542/570/575/609-619） |
| `sbus_stats` 单例（stats.py:8/93） | 60s 秒桶帧统计/丢包/告警 | rc 主循环线程：`Sbus.read` 回调 `sbus_stats.feed`（sbus.py:107-109） |
| `netrc._state`（netrc.py:19） | 网络遥控通道/seq/时间戳/丢包 | netrc 接收线程（netrc.py:36 起 `_loop`，netrc.py:41；读侧 `snapshot()` 持锁，netrc.py:69） |
| rc 循环局部（bridge.py:318-358） | sbus/sv1/svs 装配、jf_n 采样计数 | rc 主循环线程（仅本线程可见，无共享） |
| `_LOG_BUF`/`_EVENTS`（telemetry.py:9/24） | 600 行日志环 / 10 条事件环 | 全线程 append（`deque.append` 原子，设计豁免单写者） |
| `pa_recv` 模块级（pa_recv.py:47-53） | 对讲/播放器/采样率 | go2-audio 独立进程（P0-1），不与桥共享内存 |

**待登记项（遗留缝隙，进 TODO H）**：
1. `state.web_armed` 双写点：webserver WS 线程（webserver.py:217/223/292）与
   `FsmActive._sync`（bridge.py:167）各写一份，值语义一致但写点有两个 —— 待收敛为单写点。
2. `state.last_cmd` 跨线程 dict：创建与 `result` 原地改在不同线程 —— 单键赋值 GIL 原子、
   有队列 happens-before，暂安全，但按 ADR-0001 口径需登记定案。

**历史问题已收敛**：旧"同一语义（解锁/模式规则）在 rc_loop 局部、`Dog.set_mode`、`trick`
特判三处各一份"的缝隙，已随旧路径删除并入 `DogFsm` 单一状态机（core/fsm.py，表驱动 42 例锁定）；
状态镜像由 `FsmActive._sync` 单点写回 `state`，`/state` 与网页端字段口径不变。

## 7. 板级差异（必须记录，否则换板必踩）

| 项 | dog2 板 | dog3 板 |
|---|---|---|
| 家里 WiFi IP | `.51` | `.52` |
| 狗内网 | `192.168.123.50`（每狗独立网段） | 同左 |
| R 声道输出级 | **硬件损坏**（10-10 判死）→ 音频只走 L（`PA_STEREO=False`） | 完好，功放输入接 R → 双声道同数据 |
| 航模继电器接线 | 接 **pwm2**（16脚） | 接 **pwm1**（15脚） |

> 继电器控制已统一（2026-10-24）：两台都 B 键(ch5) 触发，`RelayPwm` 把 PWM1+PWM2
> **双路同值并出**（1950/1050 两档，>1650 吸合的 RC 解码型），接线差异不再进入代码与配置
> ——`_deploy_dog2/config.py` 与真身 config 完全一致，剩余差异只有 `config_local.py` 的
> `PA_STEREO`。单路 PWM export 失败时 Servo 自身降级空转，另一路照常工作。

> 音频差异由板上 `config_local.py`（`OVERRIDES`，不进 git）收敛：dog2 写 `PA_STEREO=False`，
> 变体目录 `_deploy_dog2/` 自带这份 config_local，全新板随变体部署即可拿到正确声道策略。
> 继电器双路并出后部署不再需要区分两台板（同一份 config 都对）。

## 8. 故障自愈矩阵

| 故障 | 机制 | 出处 |
|---|---|---|
| 狗断线/拔网线 | 载波+9991 看门狗强拆会话，3s 重连 | link.py:257-292（`_watch_link`） |
| 狗会话半死（信令活/指令黑洞） | GetSpeedLevel 心跳 2 次无回包即重连 | link.py:295-326（`_heartbeat`） |
| 桥事件循环停摆 | rc_loop 检测 `loop_ts` 冻结 ≥10s 报警 | bridge.py:307-315（`_loop_alive_check`） |
| SBUS 坏帧 | FSM `frame()` 坏帧只 hold：站定等待恢复，不上锁不趴下 | core/fsm.py:471-478 |
| SBUS 断流 0.25s | `safety()`：继电器回安全位 + StopMove 站定 | core/fsm.py:615-618 |
| SBUS 断流 30s | 上锁、保持姿态、不趴下（`FS_DAMP_S`） | core/fsm.py:619-625 |
| 串口拔插 | 每 3s 重开，重开成功 `serial_reopened()` 强制重新解锁 | bridge.py:330-337 |
| 桥进程崩溃 | systemd 5s 拉起 | go2-bridge.service |
| 音频进程崩溃 | go2-audio 独立服务自愈，不影响遥控 | go2-audio.service（P0-1 已落地） |
| 板 WiFi 僵死 | wifi_watchdog（丢包>50%×2 轮重连）；驱动重载未做 | TODO |

## 9. 目标与剩余工作

拆分与旧路径删除已落地（2026-10-31，本文 §3/§4），架构目标中"单一 `fsm.py` 纯逻辑状态机、
RC/网络遥控/网页/App 都只是事件源、音频独立进程"已全部成为现状。剩余目标：

- `FSM_MODE`/`FSM_SHADOW`/`_DENY_KEYS` 暂留字段移除（TODO H）；
- `_deploy_dog2` 变体收敛：变体只留 config_local 差异，旧副本清理（待用户点头，TODO H）；
- 鉴权配置化启用：NETRC_TOKEN / WEB_TOKEN / PA_ALLOW 机制已在（languard），只差配置；
- 板 WiFi 驱动重载方案与上装 12V 供电口硬件根治（TODO A0/B）。

**上装供电拓扑（10-20 事故定位）**：上装板/飞控/接收机由狗 M8 扩展线的对外 12V 供电；
第一次起立被第二条指令打断会弹起，电流冲击让该 12V 口保护性断电（狗本体不受影响）。
软件侧互锁+状态驱动起立已落地（core/fsm.py，`not rising` 才发步态）；硬件侧根治方案见 TODO A0。

**验收标准**：`kill -9` go2-audio 不影响遥控、`kill -9` 桥由 systemd 拉起；`/state` 能还原现场
（含狗自报姿态）；`test_fsm_table.py` 42 例覆盖所有边沿 + `tools/check_deps.py` 依赖守门；
任一台板可只改 `config_local.py` 完成差异化。
