# go2-bridge 子项目约定

## 目的

板子作遥控桥：G12 遥控器 SBUS（或 App 网络遥控）→ 板子解码 → WebRTC（MCF 指令）→ 宇树 Go2，
附浏览器遥控页（8000 端口）、喊话/预设音频库、动作面板。
API 全部按官方库 legion1581/unitree_webrtc_connect 核实，不参考 `../go2rc/` 的参数。

## 文档地图（单一职责，改代码必须同步改对应文档）

- **README.md** — 面向使用者：是什么、接线、部署、使用。
- **CLAUDE.md**（本文）— 面向协作者/后续 agent：约定、红线、基线、验证纪律。
- **ARCHITECTURE.md** — 面向开发者：现状/目标架构、线程模型、协议表、状态分布、板级差异。
- **OPTIMIZATION.md** — 改造执行清单（P0/P1/P2，每项含改法/验收/回滚）。
- **TODO.md** — 未完成/阻塞台账；完成项进 git log，不再留在 TODO。
- **docs/adr/** — 决策记录（ADR）：回答"为什么是这样"。改安全/协议/并发语义前先查对应条目；
  做出代码本身看不出的取舍时照 `0000-模板.md` 新增一条，编号递增不改写。

规则：基线、协议、板级差异任何一项变化，必须同一次提交更新本文与 ARCHITECTURE.md；
一次性诊断细节不写进本文（放 `sbus_diag/` 或 `research/`）。

## 目录与部署

- `go2-bridge/` 是**源码唯一真身**；`go2-bridge-原版SBUS/`、`go2-bridge-板上可用版-20260929/`
  是历史发行快照（不再手改，由 `tools/make_release.py` 重新生成，见 OPTIMIZATION P1-2）。
- 板上 `/home/pi/go2-bridge` 只是部署副本；部署一律 `BRIDGE_PW=<板子密码> python deploy_pc.py`
  （上传+md5+板上语法门+重启+验活+/state 冒烟）。`--check-only` 只读对比。
- 发布组装 `tools/make_release.py` 的部署集差集校验按路径加载 deploy_pc，PC 侧因此需要
  `pip install paramiko`（部署工具链同款依赖；板端 requirements.txt 不含它）。
- 部署尾部自动做**狗侧 EEE 自检**（best-effort 不影响门禁）：经板子 paramiko 直连隧道
  （jump host + 第二 Transport）SSH 进狗，EEE 开着就关 + 补 `/etc/network/if-up.d/eee-off`
  持久化（EEE 链路闪断根因见 TODO A）。狗密码取 `DOG_PW` 环境变量，否则读本机
  `~/.zcode/credentials.md` 的"狗 SSH"条目。**EEE 是每台狗一份的配置**：随狗出厂系统回 enabled，
  换狗/重置必须重做（自检自动兜）；2026-11-02 新狗首部署曾因旧 pty 二级跳超时静默漏网，已改隧道姿势。
- **开机自检（2026-11-04 加）**：`selfcheck.py` 启动 ~25s 后把 wlan0 关联/NM home 档案/
  wifi_watchdog/eth0 载波/磁盘可写/gas getty 占用/双服务/SBUS/狗链/EEE 部署标记 逐项吐进
  日志与 journal（起因：烧卡后 WiFi 档案丢了没人发现）。`SELFCHECK_DELAY_S` 控制延迟，0=关。
  EEE 真身状态在狗上，板侧只认 deploy_pc 守卫落的 `.eee_ok` 标记。
- **气体传感器（2026-11-03 dog4 加装）**：`gas.py` QZKJ 系列 UART Modbus(9600 8N1 从机 0x01)
  读取线程，`/state.gas` 暴露浓度/温度/状态。非安全件只读；默认 `GAS_ENABLED=False`，
  接了传感器的板子经 config_local 开启，且须停用 `serial-getty@ttyS0`（传感器独占串口0）。
  接线铁律：模块 R(收)接对端 TX、T(发)接对端 RX——R/T 交叉，接反只会听到回声。
- `BRIDGE_HOST` 支持逗号分隔多地址（默认 `192.168.123.50,192.168.8.52`），
  板子双路会轮流闪断，部署脚本逐地址试 2 次（P1-1）。
- 每次部署把 `git rev-parse --short HEAD` 写成板端 `VERSION`（无 git 的发行快照用目录自带
  `VERSION`），`/state.ver` 读它；重启后 10s 内拉 `/state`：服务非 active / `/state` 不可达
  =硬失败，`link`/`sbus.ok`/`audio.ready` 异常只打 WARN（退出码仍 0）。
- `archive/` = 一次性诊断脚本；`sbus_diag/` = 诊断日志正式归档；`sbus_diag_tmp/` 为落盘现场（gitignore）。
- **新狗上门**：全流程 runbook 见 `docs/NEW-DOG-ONBOARD.md`（烧卡→装机→EEE→遥控器→验收，
  含分工表与 2026-11-02 实踩坑全集；通道标定是人 manual 的活，agent 只提醒）。

## 板子清单（2026-10-31 更新）

| 板 | 家里 IP | 狗内网 | 板端基线 | 备注 |
|---|---|---|---|---|
| 哨兵S1(主测) | 192.168.8.53 | 192.168.144.10 | 随部署滚动（10-31 在役） | 当前主测板; 部署须显式 `BRIDGE_HOST=192.168.8.53`（脚本默认列表不含它, 防裸跑烧错板） |
| dog2 | 192.168.8.51 | 192.168.123.50 | `ce7f5be`（10-24 待部署） | R 声道输出级损坏→音频 L-only；上装带飞控，航模继电器接 pwm2 |
| dog3 | 192.168.8.52 | 192.168.123.50 | `ce7f5be`（10-24 待部署） | 双声道完好（功放接 R）；继电器接 pwm1 |
| dog4(新) | 192.168.8.54 | 192.168.123.50 | `3fd5ec2`（11-02 在役） | 新狗首板：setup4_newdog.py 全套装机；TF 卡曾闪一次 I/O 只读（重启恢复，再犯换卡）；SBUS 通道待重标（新接收机） |

- **继电器已统一（2026-10-24）**：两台都 B 键(ch5) 触发，PWM1+PWM2 **双路同值并出**
  （B高=两路 1950us 吸合 / B低=两路 1050us 断开，>1650 分界的 RC 解码型航模继电器）——
  dog2 接 pwm2、dog3 接 pwm1 都能吸合，`_deploy_dog2/config.py` 与真身 config 完全一致，
  键位差异退役；剩余板级差异只有音频声道（`config_local.py` 的 `PA_STEREO`）。

- 网络：eth0=123.50 + 144.10 别名（netplan 管，**不动**）；wlan0=NetworkManager 双档案
  （主 WiFi 静态优先 / 手机热点 DHCP 兜底），NM 省电关 + `wifi_watchdog.service`。
  热点连接上限 2（PC+pad 占满时板子进不来）。
- **跨板部署前必须确认差异**：B 键 vs D 键继电器、双声道 vs L-only（已由 `config_local.py` 的 `OVERRIDES` 收敛，见 P1-3）。
- **10-20 安全行为变更（P0-4）**：解锁不再自动趴下（趴下只认 C 下降沿）；趴/起立 2 秒互锁；
  A 键解锁去抖；开机 5 秒起立保护期；起立完成以狗自报 `body_height` 稳定为准（订阅
  `rt/sportmodestate`/`rt/lf/sportmodestate`），不再写死 3 秒；起立中不发 Move。
  真机首起立必须用黑匣子确认 `stand_up → balance(why=stable)`，见 TODO A0。

## 配置

- 所有可调参数只在 `config.py`；板上差异用 `config_local.py`（`OVERRIDES` 字典，不进 git）。
- **FSM 执行路径**：DogFsm 是唯一指令源（2026-10-31 起正式替换；**旧 Dog/rc_loop/FsmShadow
  已于 10-31 深夜随拆分分支删除**，见 ADR-0004 后续记录）。`config.FSM_MODE`/`FSM_SHADOW`/
  `_DENY_KEYS` 三条目仅暂留防板上旧 config_local 报 AttributeError，已无生产读取方，下版本移除。
- 通道映射与标定值（1002/720、172/1811）是实测假设，**换遥控器/接收机必须重跑 `sbus_monitor.py` 重标**。
- systemd 自启：`go2-bridge.service`；音频拆分后另有 `go2-audio.service`（P0-1）。
  改配置后 `sudo systemctl restart go2-bridge`（音频配置另重启 `go2-audio`）。

## 验证纪律

- 改完必跑（本地无 audioop 的 Python 用垫片）：
  `python run_test_shim.py test_sbus.py`、`test_link_sim.py`、`test_reconnect_sim.py`；
  网络遥控版另加 `test_netrc_sim.py`、`test_heartbeat_sim.py`；专项 `test_unlock_sim.py`、`test_pending_clear.py`。
  状态机改造后另加 `test_fsm_table.py`（P0-3）；安全改造后另加 `test_safety.py`、`test_stand_state.py`、
  `test_journal.py`（P0-4）；指令队列改动另加 `test_queue.py`（P0-3）；板级声道策略 `test_pa_stereo.py`（P1-3）；
  FSM 执行路径改动另加 `test_fsm_mode.py`（active/webface 两用例；shadow 用例随旧路径删除退役）；
 入口/惰性 import 改动另加 `test_entry_alias.py`（见 ADR-0008：`__main__` 双实例陷阱）；
 气体传感器(gas.py)改动另加 `test_gas.py`；开机自检(selfcheck.py)改动另加
 `test_selfcheck.py`。
- 板上日志：控制台「☰ 日志」面板（GET /logs）；诊断端点 `GET /state`（P0-2 落地后）。
  journalctl 必须按 PID/时间过滤（板子无 RTC，直接 `-n` 会被假时间戳旧日志掩埋）：
  `journalctl -u go2-bridge _PID=$(pgrep -f bridge.py) -o short-monotonic`。
- 连狗相关改动先用 `deploy_pc.py --check-only` 对比；真机未验证的行为必须明确告知用户。
- 每阶段结束跑 OPTIMIZATION.md 末尾的**真机回归清单**并打 `board-good-YYYYMMDD-N` tag。

## 契约兼容三规则（15001 行协议 / WS 消息；15001 依据 ADR-0007，WS 同策略依据同仓同部署）

1. **只增可选字段**：追加字段放行，老客户端按既有解析忽略多余字段——这是默认演进方式。
2. **废弃字段保留占位**：语义作废不改名不删除、不挪位置，直到破坏性升级窗口一并清理；不设固定保留期。
3. **破坏性变更 = App 同步升版**：改字段位置/语义/删除必须 `APP_VERSION`（android-app/PaService.java）
   升版 + 重打 APK + `deploy_page.py` 上架——其 **APP 路径 / VERSION_JSON 版本 / 页面文案 v 号三处
   硬编码需同步手改**（10-31 曾漂移 11 版未被发现，靠本条防复发），新版铺开前板端保留旧格式。

依据：网页与桥同仓同部署不会失配；App 无强制更新通道、用户单一，按"字段兼容"而非"时间窗"管理，
原设想的"3 个月双版本"按此砍掉。

## 安全红线

- 初期限速 VX≤0.5 m/s；失控保护口径（09-29 定）：运动中丢信号 → 0.25s 停走站定；
  App 网络源 ≈0.55s；30s 真失联上锁**保持姿态不趴下**；趴下只认显式命令/档位（三段开关拨趴、
  A 键上锁、网页 disarm/damp），网页退出=解除控制+停走保持姿态。
- Go2 固件锁 1.1.x（1.1.1~1.1.14 免密钥），**App 里严禁开固件自动升级**；≥1.1.15 需逐机取 AES key。
- 联调架空优先，远离楼梯、水域、人群；浏览器页关闭=解除网页控制并停走（保持姿态，10-29 起不再自动趴）。
- 涉及狗姿态的改动（解锁/起立/趴下/挡位/动作）必须走影子对照或真机空载验证，禁止直接上真机试。

## 安全关键函数（SAFETY-CRITICAL，唯一真相源）

下表函数/常量直接决定狗的物理安全。**修改要求：主会话（人机共同）审查 + 本地全量测试 +
真机空载验证**，回归清单（`docs/REGRESSION-CHECKLIST.md`）必跑；禁止绕过测试直接改。

| 机制 | 真相源位置 | 口径 | 依据 |
|---|---|---|---|
| 失控保护（丢帧停走） | `core/fsm.py safety()`；active 执行层 `bridge.py FsmActive.safety()`；App 源新鲜窗 `netrc.py FRESH_S=0.3` | SBUS 无干净帧 `FS_STOP_S=0.25s` 停走站定；App 网络源 ≈0.55s（=FRESH_S 0.3+FS_STOP_S 0.25 设计推导值）；30s（`FS_DAMP_S=30`）真失联上锁**保持姿态不趴** | ADR-0003/0005 |
| 换源首帧播种 | active 现役=`core/fsm.py frame()`（坏帧不播种+换源只记电平）；shadow/旧路径=`bridge.py` rc_loop 播种块 | 首帧只记电平不合成边沿 | ADR-0002 |
| 关网页=hold | `core/fsm.py web_cmd("closed")`+`hold()`；`FsmActive.hold()`；WS 会话 finally 钩子 | 解除接管+清轴；未被 RC 解锁（armed=False 且在链）再 hold 保持姿态；永不趴 | ADR-0003 |
| 趴/起立互锁 | `config.py MODE_DWELL_S=2.0` | 两次会发 StandDown/StandUp 的模式切换最小间隔 | 安全红线（10-20 供电口事故止血） |
| A 键解锁去抖 | `config.py ARM_DEBOUNCE_S=1.0` | 解锁上升沿去抖；**上锁下降沿永不去抖**（任何时候都能收权趴下） | 安全红线 |
| 起立保护期 | `config.py STAND_GUARD_S=5.0`；拦截点 `core/fsm.py set_mode` 保护期分支 | **进程启动** 5s 内拒绝会发 StandUp 的模式切换（已站立的狗不拦运动）；起立完成以狗自报 body_height 稳定为准（不写死时长），起立中不发 Move | P0-4 |
| languard 门禁 | `languard.py` | 配置 token 后必校验（hmac.compare_digest）+IP 白名单；未配置=放行 | ADR-0005 |
| 趴下触发口径 | 三段开关拨趴（C 下降沿）、A 键上锁、网页 disarm/damp（均为显式命令/档位） | 会话关闭/超时/失控保护/丢帧均不趴 | ADR-0003 |
