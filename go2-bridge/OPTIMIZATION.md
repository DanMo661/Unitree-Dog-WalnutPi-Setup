# go2-bridge 改造清单（2026-10-17 制定）

> 本文是**唯一**的改造执行清单。每项都给出：为什么改、改哪里、怎么改、怎么验、风险与回滚。
> 改动顺序按 P0 → P1 → P2。每完成一项在本文件勾掉，并把实际做法补进 ARCHITECTURE.md。
> 纪律：**先写测试/影子对照，再切换行为**；每阶段结束跑真机回归清单（见文末）并打 tag。

---

## 优先级总表

| 编号 | 事项 | 收益 | 工作量 | 风险 |
|---|---|---|---|---|
| P0-1 | 音频拆独立服务 | 音频故障不再杀遥控 | 0.5-1 天 | 低 |
| P0-2 | 新增 `GET /state` | 诊断/部署冒烟的地基 | 2-4h | 极低 |
| P0-3 | 状态机抽离 + 表驱动测试 | 根治"弹起/踩踏"bug 家族 | 1-2 天 | 中（用影子模式） |
| P0-4 | 起立安全互锁 + 狗真实姿态闭环 | 治"开机首个起立"弹起/对外供电口断电 | 0.5 天 | 中（行为变更） |
| P1-1 | 部署冒烟 + 版本号 + 双地址 | 每次部署可知成败、可定位版本 | 2-3h | 低 |
| P1-2 | 单一真身 + 快照生成 + 公开仓同步 | 消灭四份副本漂移 | 0.5 天 | 低 |
| P1-3 | 标定值/板级差异进配置 | 换遥控器、双板差异不再改代码 | 2h | 低 |
| P2-1 | App 目标 IP 可配 | 换网段不用重编 APK | 0.5 天 | 低 |
| P2-2 | 网络状态进 /state + 守护日志 | 闪断可留证 | 2h | 极低 |

---

## P0-1 音频子系统拆成独立 systemd 服务

**为什么**
`pa_recv.py` 的 3 个线程（音频接收/控制/上传）跑在 bridge 主进程里（`bridge.py:746 pa_recv.start()`）。
任何一处的未捕获异常都与遥控共享同一条命：进程死 → systemd 拉起重启 5 秒（`go2-bridge.service` RestartSec=5），期间遥控中断、继电器输出掉电。
10-17 13:50 已出现一次无 `Stopping` 记录的非正常退出（journal 证据），当时正在音频 PLAY 期间（13:48 `aplay: Device or resource busy`）。崩溃根因待 traceback 定案，但**爆炸半径必须缩小**。

**改哪里**
- `pa_recv.py`（新增 `__main__` 入口）
- `bridge.py:732-751`（main 里去掉 `pa_recv.start()` 与两个回调注入）
- 新增 `go2-audio.service`
- `deploy_pc.py:21-22`（FILES 不变，但要重启两个服务）
- `setup3_dog3.py:56-64`、`setup2_ssh.py`（上板+enable 两个 unit）

**怎么改（步骤）**
1. `pa_recv.py` 末尾加独立入口：
   ```python
   if __name__ == "__main__":
       import config, time
       from pa import Pa
       start(Pa(config.DEFAULT_VOLUME, log), config, log)
       while True: time.sleep(3600)
   ```
   `start()` 内部已有 3 线程；主线程保活即可。
2. 解耦两个回调（当前由 bridge 注入）：
   - `trick_cb`（`pa_recv.py:35`）：bridge 新增本地 UDP 口 `TRICK_PORT=15011`（config），监听 `127.0.0.1`。
     pa_recv 收到 `TRICK name` 时向 `127.0.0.1:15011` 发一行并等 1s 回包，把 OK/LINKDOWN/NOTSTAND 透传给 App（协议对 App 不变）。
   - `state_cb`（`pa_recv.py:36`）：改为 HTTP 读 `http://127.0.0.1:8000/state`（stdlib `urllib`，无新依赖），
     取 `rc_armed/mode/gear` 三个字段；P0-2 先落地，本项依赖它。
3. 新增 `go2-audio.service`：
   ```ini
   [Unit]
   Description=Go2 audio (talk/preset library)
   After=sound.target network-online.target
   [Service]
   User=root
   WorkingDirectory=/home/pi/go2-bridge
   ExecStart=/home/pi/go2-bridge/.venv/bin/python pa_recv.py
   Environment=PYTHONUNBUFFERED=1
   Restart=always
   RestartSec=2
   [Install]
   WantedBy=multi-user.target
   ```
4. 部署脚本：上传 unit、`systemctl enable go2-audio`、重启顺序 `go2-audio` → `go2-bridge`（或各管各的，互不依赖）。
5. 验收：
   - `kill -9 <audio pid>` → 音频服务 2s 内自愈；`go2-bridge` 保持 active，遥控不受影响。
   - App 全链路：HELLO / LIST / PLAY / TALK / 录音上传 / TRICK（含配乐联动）/ RC 查询。
   - 六测全绿（`run_test_shim.py` 各套）。
6. 回滚：撤 `go2-audio.service`，`bridge.py` main 恢复 `pa_recv.start()` + 回调注入（保留 5 行注释说明）。

**风险**：TRICK 多一次本地 UDP 往返（毫秒级，App 2.5s 收包窗无感）；音频服务与桥争用的只有声卡，而声卡只有音频服务用。

---

## P0-2 新增 `GET /state` JSON 状态端点

**为什么**
现在只有 `GET /logs`（文本事件流）和 `/ws`（websocket，需客户端）。
10-17 排查继电器时无法用 curl 读到"SBUS 是否在进帧、当前通道值"，诊断被 websocket 卡住。
`/state` 是部署冒烟（P1-1）和 App/网页状态同步的公共地基。

**改哪里**：`bridge.py:647-729`（web_server 加路由）；`rc_loop` 里补镜像字段。

**怎么改**
1. 在 `rc_loop` 维护：`state.last_frame_t`（最近任意帧 monotonic）、`state.relay_on/relay_us`（每次 `sv1.set_us` 后写）。
2. 新路由返回 JSON：
   ```json
   {
     "ver": "e4de755", "uptime_s": 123.4,
     "link": "up", "link_gen": 3,
     "mode": 2, "rc_armed": false, "web_armed": false, "gear": 2,
     "action": null, "action_left": 0.0,
     "sbus": {"ok": true, "clean": true, "fps": 99, "last_iv": 10.1,
              "frame_age": 0.02, "total": 1, "fs": 0, "lost": 0, "gap": 0,
              "ch": [1002, ... 16 个]},
     "net": {"ok": false, "rx": 0, "lost": 0, "seq": -1},
     "relay": {"on": false, "us": 1050},
     "audio": {"ready": true, "volume": 25, "talk": false, "playing": null}
   }
   ```
   数据来源：`state` + `sbus_stats.snapshot()` + `netrc.snapshot()` + 新增镜像字段。
3. 音频拆进程后，音频状态由 `pa_recv` 每秒写 `/run/go2-audio.json`，`/state` 读取合并（文件不存在则 `ready:false`）。
4. 验收：curl 1 次拿全量；拔 SBUS → `frame_age` 变大、`ok:false`；B 键切换 → `relay.on` 翻转；重启后字段完整。

**注意**：`/state` 只读，不触发任何硬件写操作。

---

## P0-3 状态机抽离（本清单最重要的一项）

**为什么**
控制状态散在五处：`state` 全局镜像（`bridge.py:92-107`）、`Dog` 内部（`bridge.py:375-510`）、
`rc_loop` 局部变量（`bridge.py:513-526`：`armed/prev_high/stand_prev/last_gear/gear_sent/link_gen`）、
`netrc`（`netrc.py:16`）、`pa_recv`（`pa_recv.py:27-34`）。
所有危险 bug 都属于同一族：10-10 趴下切挡弹起、10-14 趴着解锁弹起、10-17 起立 3s 内拨 C 迟到 BalanceStand。
每修一个边角，下一个分支再冒一个——因为**没有单一入口**。

**改哪里**：新增 `fsm.py`；`bridge.py:513-644`（rc_loop 改为事件构造 + FSM 输出执行）；逐步废弃 `Dog` 内部散装状态。

**怎么改**
1. `fsm.py` 只放纯逻辑，零硬件、零线程依赖：
   ```python
   class DogFsm:
       def __init__(self): ...   # armed/mode/gear_sent/prev_high/stand_prev/link_gen/pending/cooldown/moving
       def step(self, ev) -> list[Cmd]: ...
   ```
   事件（一个 dict/dataclass 都行）：
   - `frame(ch, fs, lost, source)`
   - `link(gen)`（代数变化 = 重连）
   - `safety(gap_clean, now)`
   - `tick(now)`
   - `web(cmd)` / `trick(name, dur)`
   输出 `Cmd`：`("submit", "StandUp", {})`、`("relay", 1950)`、`("log", "...")` 等，由 rc_loop 执行。
2. **影子模式迁移（关键）**：rc_loop 里旧逻辑照跑，同时喂新 FSM，逐帧比较输出指令序列；
   有差异打 `FSM 差异:` 日志（含事件+两边输出）。真机跑 1-2 天使用场景（解锁/起立/走/挡位/动作/断链），零差异后切换。
3. 现有规则全量搬入，逐条注明来源（`bridge.py:xxx` 行号）：
   - 解锁只认 A 上锁→解锁边沿（574-586）
   - C 起立只认下→上边沿；站/走时 C 在上补同步（587-597）
   - 趴下时禁止发步态指令，只记账（598-606）
   - 模式切换清空 pending（388-394）
   - 坐姿特判：3→0 静默、3→2 忽略（395-403、413-415）
   - 坏帧站定 / 30s 失联上锁不趴（626-636）
   - 首连零指令 + 断链重连 StopMove（493-510）
4. 新增 `test_fsm_table.py`：表驱动，输入事件序列 → 期望 Cmd 序列。
   首批用例直接复用 `test_unlock_sim.py`、`test_pending_clear.py` 的场景，再补：
   A 连发抖动、C 连发抖动、坏帧夹在边沿中间、重连瞬间的陈旧电平、坐姿+挡位、动作冷却中拨杆。
5. 验收：旧六测全绿 + 新表驱动全绿 + 真机回归清单全过。

**风险与回滚**：影子模式零行为变化；切换后若异常，`git revert` 单提交即回旧 rc_loop。

---

## P0-4 起立安全互锁 + 狗真实姿态闭环（2026-10-20 事故）

**为什么**
10-20 真机：开机后第一次"解锁→趴下→起立"时狗抽搐一下，上装板（由狗 M8 扩展线 12V 供电）整体掉电，
狗本体仍有电；此后趴下/起立/动作全部正常。黑匣子最后一行是写了一半的 JSON，断电前约 2 秒的
`cmd/arm/mode` 记录还在页缓存里丢了。结论：**不是供电余量**（否则每次起立都会断），是第一次起立
期间插入了第二条指令（旧代码 `StandUp` 后写死 3 秒补 `BalanceStand`，第一次起立更慢，3 秒时狗还没
起完），狗被弹起 → 电流冲击 → 狗对外供电口保护性拉闸。

**改哪里 / 怎么改（已落地）**
- `config.py`：`MODE_DWELL_S=2.0`（趴/起立反向动作最小间隔）、`ARM_DEBOUNCE_S=1.0`（A 键解锁去抖）、
  `STAND_GUARD_S=5.0`（开机起立保护期）；`DOG_STAND_HEIGHT=0.22`、`DOG_HEIGHT_STABLE=0.004`、
  `DOG_STATE_STALE_S=1.0`、`STAND_TIMEOUT_S=6.0`、`DOG_STATE_WAIT_S=2.0`。
- `bridge.py`：`Dog.set_mode` 返回 bool，被互锁/保护期拦下时不发指令；`rc_loop` 用 `stand_req/lie_req`
  标志每帧重试；**解锁不再自动趴下**（趴下只认 C 下降沿）；A 键解锁上升沿去抖（上锁下降沿永不去抖）。
- **狗真实姿态**：连接后订阅 `rt/sportmodestate` + `rt/lf/sportmodestate`（旧固件两路取其一），
  `/state.dog` 暴露 `mode/body_height/gait/velocity/age/fresh`。首连先等第一条姿态（≥2s），
  按 `body_height` 决定本地 `mode`，不再拍脑袋假定站立。
- **状态驱动起立**：`StandUp` 后不再固定 3 秒，改由 `_maybe_balance()` 看狗自报 `body_height`：
  0.6s 窗口内稳定（波动 < 4mm）且高于 0.22m 才补 `BalanceStand`；无姿态数据回落旧行为 3 秒；
  有数据但一直不稳则 `STAND_TIMEOUT_S=6s` 兜底。起立流程中 `move()` 不响应。
- `journal.py`：`event` 与非 Move `cmd` 记录 flush 后 `os.fsync`（Move/高频 frame 只 flush）；
  `.running/.clean` 停机标记 + `last_stop()`/`tail(n)`。
- `deploy_pc.py`：验活改为轮询 `/logs` 最多 24s（uvicorn 要等首连后才起，固定 6s 会误判）。
- 调试台（`static/sbus.html`）：系统与事件面板（上次关机/桥与音频 PID/最后命令/最近 10 条事件/
  load/内存/温度）+ **狗真实姿态**行（高度/mode/步态/速度/数据龄/起立中）。

**怎么验**
- 本地：`test_fsm_table.py` 35/35、`test_safety.py` 10 组、`test_stand_state.py` 5 组、
  `test_journal.py` 及 7 套仿真全绿。
- 真机：部署后 `/state.dog` 有 `n>0`、`height≈0.32`、`fresh=true`；开机首次起立时黑匣子应记录
  `stand_up` → `balance(why=stable, dt≈?)`，且 `dt` 小于 3 秒或按实际起立时长；调试台"上次关机"
  在正常 `systemctl stop` 后显示"正常关机"。

**风险与回滚**
行为变更（解锁不再自动趴下、起立等待稳定）对操作习惯有影响；回滚 `git revert` 本提交即可。
互锁只降低触发概率，**不改变狗对外供电口的保护行为本身**；若实测仍断电，按 TODO 的硬件方案
（独立供电/储能/共地）继续。

---

## P1-1 部署冒烟 + 版本号 + 双地址重试

**为什么**
`deploy_pc.py:108-118` 只验"服务 active + 日志无 Traceback"，不知道板上到底跑的是哪版、SBUS/狗/音频活着没；
板子有线口与 WiFi 会轮流闪断（10-17 一天内多次），单地址部署经常被打断。

**怎么改**
1. 部署时 `git rev-parse --short HEAD` 写入板端 `VERSION` 文件（随 FILES 上传），`/state` 返回 `ver`。
2. `BRIDGE_HOST` 支持逗号分隔多地址，逐个尝试（默认 `192.168.123.50,192.168.8.52`），每个 2 次。
3. 重启后轮询 `/state` 最多 10s：
   - 硬失败（退出码非 0）：服务非 active / `/state` 拿不到。
   - 警告（退出码 0，打印醒目 WARN）：`link != up`（狗没开？）、`sbus.ok=false`（接收机没开？）、`audio.ready=false`。
4. 验收：故意拔 SBUS → 部署提示 WARN；正常部署 → 打印 `ver/fps/link/relay` 摘要。

**落地（2026-10-21，DONE）**
- `deploy_pc.py`：VERSION 写临时文件后与运行文件同套 md5/上传/校验（不脏工作区）；无 git 的发行快照回退读目录内 `VERSION`；
  多地址逐试 2 次；重启后 `/state` 10s 轮询（服务非 active/不可达=硬失败，link/sbus/audio 异常=WARN 不失败）；
  现有 md5/语法门/验活（`/logs` 等待 uvicorn 起）全部保留。`bridge.py:_ver()` 已读 VERSION，`/state.ver` 无需改动。
- 注意：工作区有未提交改动时只提示、VERSION 仍记 HEAD。

---

## P1-2 单一真身 + 快照生成 + 公开仓同步

**为什么**
当前四份代码：`go2-bridge/`（真身）、`go2-bridge-原版SBUS/`、`go2-bridge-板上可用版-20260929/`、
GitHub 公开仓快照（`Unitree-Dog-WalnutPi-Setup/go2-bridge/`，仍停在 D 键继电器/无弹起修复/无立体声修复）。
手工同步必然漂移——公开仓装出来的狗行为与现役不同。

**怎么改**
1. 新增 `tools/make_release.py`：从真身拷贝**运行集**（bridge/config/sbus/servo/pa/pa_recv/netrc/static/两个 service）到指定快照目录，附带 `VERSION` 与 `git rev-parse HEAD`。
2. `go2-bridge-原版SBUS/` 与 `板上可用版-20260929/` 降级为"发行快照"，**禁止手改**，由上述脚本重新生成（旧版本保持原样存档）。
3. 公开仓同步流程（放在代码稳定后执行）：
   - `make_release.py` 输出到 `Unitree-Dog-WalnutPi-Setup/go2-bridge/`
   - 跑公开仓内六测 + `sbus_monitor` 冒烟
   - 核对 SOP/MANIFEST 引用的文件名与参数（如 `--home-ssid/--home-gw`、`go2-audio.service`）
   - 用户确认后再 push（红线：对外推送先过目）
4. Tag 规则：`board-good-YYYYMMDD-N`，只在**真机回归清单全过**后打；回退 = `git checkout <tag>` + 对应版本 `deploy_pc.py`。
5. CLAUDE.md 增加"发行台账"表：tag / 日期 / 目标板 / 验证人 / 备注。

---

## P1-3 标定值与板级差异进配置

**为什么**
- `axis()` 的 1002/720（`bridge.py:53-58`）、`pos()` 的 172/1811（61-63）、按键阈值 1002（615）写死在函数里。
- 两板硬件不同：dog2 板 R 声道输出级损坏（10-10 判定，对策=信号只走 L）；dog3 板两声道完好、功放接 R（10-17 判定，对策=双声道同数据）。
  同一份代码不可能同时最优，必须板级配置。

**怎么改**
1. `config.py` 增加显式标定项并替换函数内魔法数：
   ```python
   AXIS_MID, AXIS_SPAN = 1002, 720
   POS_MIN, POS_MAX = 172, 1811
   KEY_THRESHOLD = 1002
   ```
2. `config.py` 末尾支持板级覆盖（不进 git）：
   ```python
   try:
       import config_local
       for _k, _v in config_local.OVERRIDES.items():
           globals()[_k] = _v
   except ImportError:
       pass
   ```
3. `pa_recv.py` 两处 `tostereo` 改为读 `getattr(config, "PA_STEREO", True)`：
   dog3 默认 True（双声道同数据）；dog2 的 `config_local.py` 写 `OVERRIDES = {"PA_STEREO": False}`（L-only，沿用 10-10 对策）。
4. 部署脚本上传 `config_local.py`（若存在），`deploy_pc.py` FILES 动态加。
5. 验收：两板各跑一次音频验收（dog2 只 L、dog3 双声道）；标定值改动只动 config。

---

## P2-1 App 目标 IP 可配

**为什么**：`PaService.java:74`、`PaSend.java:34` 写死 `192.168.144.10`；换网段/换架构要重编 APK。

**怎么改**
1. SharedPreferences 增加 `board_ip`（默认 `192.168.144.10`），悬浮窗/主界面加一行"板子地址"。
2. 所有 UDP/HTTP 目标改为运行时读取；未设置时用默认值。
3. 验收：G12 上改 IP 后无需重装即可连板。

---

## P2-2 网络状态进 /state + 守护日志

**为什么**：eth0 狗口 20 分钟闪断与 board WiFi 僵死是当前最大运维干扰，现有看门狗只重连不留证据。

**怎么改**
1. `bridge.py` 每秒记录 `carrier` 变化与临时掉线时长到 `/logs`（只在变化时打）。
2. `/state` 增加 `net_if: {"eth_carrier": 1, "wifi_rssi": -42, "gw_ping_ms": 3}`（失败为 null）。
3. `wifi_watchdog` 脚本把最近一次重连原因写 `/run/wifi-watchdog.json`，`/state` 合并。
4. 验收：拔网线/断 WiFi 后 `/state` 与 `/logs` 都能看出时间点与原因。

---

## 执行顺序与验收门

```
阶段 A（P0）: P0-2 → P0-1 → P0-3
  门禁: 六测全绿 + 音频/遥控互不影响 + 真机回归清单 + tag board-good-YYYYMMDD-1
阶段 B（P1）: P1-1 → P1-3 → P1-2
  门禁: deploy 冒烟可用；两板验收；公开仓快照生成并核对（push 需用户点头）
阶段 C（P2）: P2-1 → P2-2
  门禁: G12 实机改 IP 连通；闪断证据可回放
```

**先做 P0-2 的理由**：`/state` 是 P0-1（state_cb）和 P1-1（部署冒烟）的前置依赖，且风险几乎为零。

---

## 真机回归清单（每阶段结束必跑，dog2/dog3 各一次）

```
□ 上电 → WebRTC 连上 → 心跳回读挡位有值
□ A 翻拨解锁 / 再拨上锁
□ C 下→上：慢速起立（不得瞬间弹起）
□ 趴下 + A 解锁：狗不自动起立
□ 摇杆三轴：前后/侧移/转向方向正确
□ 站着切三挡不弹起；趴着切挡不起立
□ 动作面板：Sit 坐下 → C 边沿起立；Dance 配乐联动
□ B 键：继电器两档（吸合/断开），GPIO 四路跟随
□ 拔 SBUS：站定等恢复；30s 上锁保持姿态；插回自动恢复
□ 拔网线/断 WiFi：看门狗拆会话重连；恢复后可再解锁
□ 音频：预设播放 / 对讲 / 录音上传 / 音量调节
□ POST 状态：/state 字段与实物一致
```

> 回退原则：任一门禁失败 → 当阶段不合并，回退到上一个 `board-good` tag，问题记入 TODO.md。
