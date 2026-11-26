# 真机回归清单

> 唯一事实来源：`../OPTIMIZATION.md` 文末「真机回归清单（每阶段结束必跑，dog2/dog3 各一次）」。
> 本文件是其转正版：场景原文照录、仅按安全优先重排，**不新增不发明场景**。
> 配套：CLAUDE.md「验证纪律」；CLAUDE.md「安全关键函数（SAFETY-CRITICAL，唯一真相源）」已把本清单列为改动后的必跑项。

## 何时必跑

- 每次大版本部署到板子之后。
- 打 `board-good-YYYYMMDD-N` tag 之前（OPTIMIZATION.md：tag 只在真机回归清单全过后打）。

## 执行方式

- **用户主控**：操作狗 + 遥控器（含 App）；一切带物理风险的动作由人执行。
- **主会话 agent 辅助**：负责部署（`BRIDGE_HOST=192.168.8.53 BRIDGE_PW=<板子密码> python deploy_pc.py`）、取证（/state、/logs、/journal?n=、journalctl）、记录（填文末运行记录表）。子代理禁碰部署（docs/templates/AGENT-TASK-BRIEF.md §3），部署由主会话执行。
- 失控/断链类场景（#6/#7）安全降级：无架空条件时=平地空旷+最低速挡+人在狗侧一臂内（可随时抱起/断电）；严禁在楼梯、水域、人群旁测。
- 范围：源清单口径为 **dog2/dog3 各一次**（原清单表述）；当前主测板为哨兵S1（见 CLAUDE.md 板子清单，部署须显式 `BRIDGE_HOST=192.168.8.53`）。

## 判定

- 12 项**全过**才可打 tag。
- 任一未过 → 当阶段不合并，回退上一个 `board-good` tag，问题记 `../TODO.md`（OPTIMIZATION.md 回退原则）。

## 场景表

- 「场景」列 = 源清单原文（一字未改）；「操作 / 预期」按 CLAUDE.md 与 ADR 口径展开，含糊处标注（原清单表述）。
- 排序按安全相关优先：#2~#5 是上锁路径与姿态互锁，#6~#7 是失控保护；#6/#7 需"站定"前置，故排在起立/互锁之后。
- 「本地预检」列 = 对应 `test_*.py`（`python run_test_shim.py test_xxx.py`）。**本地预检通过 ≠ 场景通过**，只是省真机轮次。

| # | 场景（源清单原文） | 前置 | 操作 | 预期 | 证据位置 | 本地预检（非通过依据） |
|---|---|---|---|---|---|---|
| 1 | 上电 → WebRTC 连上 → 心跳回读挡位有值 | 板子+狗+遥控器/接收机供电正常（#2 起要用 A 键） | 板子上电，等自启与建连 | WebRTC 连上；心跳回读挡位有值；/state.ver = 本次部署 VERSION，link/sbus.ok 正常 | /state、journalctl（按 PID 过滤）、journal kind=link | test_heartbeat_sim.py |
| 2 | A 翻拨解锁 / 再拨上锁 | #1 过；狗在安全位 | A 键翻拨解锁，再拨回上锁 | 解锁后接受控制；上锁趴下（唯一趴下路径，安全红线 / ADR-0003）；上锁下降沿永不去抖 | journal（arm 边沿）、/state.arm_blocked、视频 | test_unlock_sim.py、test_safety.py |
| 3 | 趴下 + A 解锁：狗不自动起立 | #2 过（上锁后狗已趴） | 狗趴着，只拨 A 解锁，不动 C | 保持趴下，不自动起立（P0-4：解锁不改变姿态） | journal fsm 事件、视频 | test_unlock_sim.py |
| 4 | C 下→上：慢速起立（不得瞬间弹起） | #3 过，狗趴着 | C 拨下→上 | 平滑慢速起立，不瞬间弹起；起立完成以狗自报 body_height 稳定为准（P0-4），起立中不发 Move | journal（stand_up → balance why=stable）、视频 | test_stand_state.py、test_safety.py |
| 5 | 站着切三挡不弹起；趴着切挡不起立 | #4 过 | 站立状态切三挡；C 拨下趴下后再切挡（验证趴着切挡不起立） | 切挡不触发起立/趴下；趴/起立 2 秒互锁（config.MODE_DWELL_S） | journal 模式切换记录、视频 | test_safety.py |
| 6 | 拔 SBUS：站定等恢复；30s 上锁保持姿态；插回自动恢复 | #5 过；若 #5 终态为趴，C 拨上重新起立至解锁站立（架空优先，无架空按执行方式降级条款） | 运动中拔 SBUS → 等待 → 插回 | 丢帧 0.25s 停走站定（App 网络源 ≈0.55s）；30s 真失联上锁但**保持姿态不趴**；恢复分两支：30s 内插回→自动回可控；已满 30s 上锁→插回**保持上锁**，需 A 键重新解锁（串口重开要求重新解锁，防插拔误触发） | journal 坏帧/失联时间轴、视频 | test_link_sim.py、test_safety.py |
| 7 | 拔网线/断 WiFi：看门狗拆会话重连；恢复后可再解锁 | #6 过 | 断板子网络 → 等看门狗动作 → 恢复网络 | 会话被拆、自动重连；恢复后可再解锁接管 | journal kind=link（ev=reset）、/logs 看门狗行、/state 的 link/link_gen（重连时 link_gen 递增）、视频 | test_reconnect_sim.py、test_heartbeat_sim.py |
| 8 | 摇杆三轴：前后/侧移/转向方向正确 | #7 过，狗解锁站立（架空优先） | 依次推前后 / 侧移 / 转向 | 三轴方向与杆量一致；初期限速 VX≤0.5 m/s | 视频对照、journal Move 采样 | test_sbus.py、test_netrc_sim.py |
| 9 | 动作面板：Sit 坐下 → C 边沿起立；Dance 配乐联动 | #8 过 | 网页动作面板点 Sit；C 边沿起立；点 Dance | Sit 坐下；C 边沿起立；Dance 配乐联动播放 | 视频、journal cmd、/logs | test_fsm_table.py、test_link_sim.py |
| 10 | B 键：继电器两档（吸合/断开），GPIO 四路跟随 | #8 过 | 拨 B 键两档 | 继电器吸合/断开（双路 PWM 同值并出）；38/40/37/35 脚四路 GPIO 跟随（config.RELAY_GPIOS） | 实物观察（吸合声/万用表）、journal、截图 | test_fsm_table.py、test_fsm_mode.py |
| 11 | 音频：预设播放 / 对讲 / 录音上传 / 音量调节 | #1 过即可（不依赖站立） | 遥控页/App 依次：预设播放、对讲、录音上传、音量调节 | 四项全部可用；/state 的 audio.ready 正常 | /state（audio.ready）、录音文件/录屏、/logs | test_pa_stereo.py；协议层可先跑 `BRIDGE_HOST=192.168.8.53 python run_test_shim.py test_pa_v3.py`（板在线预检，PC 模拟 App；HOST 缺省 127.0.0.1 防误发，不带必 FAIL） |
| 12 | POST 状态：/state 字段与实物一致 | #1 过 | 读 /state 逐字段对照实物 | 挡位、电量、姿态等与实物一致；ver = 本次部署 VERSION | /state 截图 + 实物对照 | 无本地对应（板端事实） |

### 取证命令备查

- 板端日志：控制台「☰ 日志」面板（GET /logs）；诊断端点 GET /state；黑匣子回放 GET /journal?n=
- journalctl 必须按 PID 过滤（板子无 RTC，直接 `-n` 会被假时间戳旧日志掩埋）：
  `journalctl -u go2-bridge _PID=$(pgrep -f bridge.py) -o short-monotonic`
- journal 黑盒落盘：板上 `/home/pi/go2-bridge/journals/journal-*.jsonl`（config.JOURNAL_DIR 空=自动选择）

## 运行记录

| 日期 | 板端 VERSION | 结果 | 未过项 | 备注 |
|---|---|---|---|---|
|  |  |  |  |  |

- 板端 VERSION = 部署写入的 `git short HEAD`（/state.ver 读）。
- 首次全跑：部署 `ca09720` 之后的版本时（2026-11-01）。
