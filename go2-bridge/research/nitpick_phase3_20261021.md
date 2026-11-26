# 阶段 3 复查修复 — nitpick 两轮审计归档（2026-10-21）

对象：commit 85d3da0（6 条 phase-3 发现修复）→ 84bb773（一轮返修）→ b2a5cc1（二轮返修）。
方法：独立 agent 各自取 `git archive` 快照到 `D:\ZcodeRely\nitpick-audit\` 隔离实测，
真仓只读；找刺与修刺分离，主循环逐条核实后修复。

## 第一轮（陌生视角全流程，25 断言自证 checked-submit）

**0 BUG**。返修 5 条：

| # | 级别 | 发现 | 修法（84bb773） |
|---|------|------|----------------|
| 1 | DOC | ARCHITECTURE.md 还写"active 把 submit 落地"，与 send 注入矛盾 | FSM 节重写：探针口径 / send 落地 / checked-submit 契约 |
| 2 | NIT/UX | 三个仿真桩无 `loop` 属性 → 探针恒 False → 每帧假"FSM 差异"刷屏，FSM 提交纪律零覆盖 | StubLink 补 `loop=True`（刷屏清零，实测 0 行） |
| 3 | DOC | deploy_pc 用法没写 `_deploy_dog2` 变体 | 文件头补用法行 |
| 4 | NIT | 只 mkdir core，static 缺失时 sftp 裸炸 | mkdir 同时建 core+static |
| 5 | NIT | BRIDGE_PW 无 shell 转义（$;/| 注入） | `shlex.quote`（3 处 sudo -S） |

记录（不修）：gear_sent 先记后发 = 旧路径同构的 fire-and-forget，断链窗口丢一次
挡位、同挡不补发（写入 set_gear docstring）；cfg 缺键裸炸与 send 抛异常均为可接受
暴露面（影子有保险丝，active 与旧 rc_loop 同等）。

## 第二轮（回归验证 6/6 ✅ + 换角度猎杀）

返修 4 条（b2a5cc1）：

| # | 级别 | 发现 | 修法 |
|---|------|------|------|
| B1 | BUG·条件 | `_deploy_dog2` 部署依赖"板上已有 config_local"，全新/重灌板会被 PA_STEREO=True 打成双声道（dog2 右声道坏） | 变体自带 `config_local.py`（PA_STEREO=False），`.gitignore` 加 `!` 放行进 git |
| B2 | UX | 上传中途断连 = 裸 traceback（非伪成功，rc=1） | sftp 循环包 try/except → 可读 sys.exit("直接重跑") |
| B3 | UX | `set_mode(-1)` 静默穿透负索引污染 mode；9 → IndexError | 入口 `m not in (0,1,2,3)` 抛 ValueError |
| B5 | DOC | "仅兜底并告警"漏说"仍真发" | 补一句 |

勘误：一轮提交信息写"密码转义四处"，实际 sudo -S 共 3 处（第 4 处替换是 qpw 定义行）。

## 验证证据（关键项）

- 快照离线套件 14/14 全绿（test_pa_v3 需真板，按约定跳过）；41/41 表驱动。
- checked-submit 25 断言：StandUp 失败绝不进 rising、之后绝不向趴狗补 BalanceStand；
  set_mode 三方向失败不提交；BalanceStand 失败保持 rising 逐包重试；trick 失败不收
  冷却；Move/StopMove 失败不认账。
- config_local 四组：缺失放行 / 覆盖生效 / FSM_MODE 拒绝且 `_fsm_mode()` 回落 shadow /
  坏档当场炸。
- deploy_pc 假桩 8 场景：md5sum rc/缺文件、双 restart rc、/state.ver 不符、/logs 空、
  Traceback 全部正确中止；无"打印成功但没生效"路径。
- 三仿真补 loop 后 0 行"FSM 差异"、断言全过、无旧路径分叉。

未覆盖：真机 SSH/UDP/WebRTC 集成（板/狗不达）；探针与 DogLink 重连窗口的微秒级
竞态（静态判可接受瞬态）。
